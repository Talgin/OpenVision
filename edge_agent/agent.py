"""OpenVision edge agent: runs on the client's LAN, pushes their cameras to our ingest.

Outbound only. Every poll it asks the API which cameras it serves
(GET /api/v1/edge/config) and keeps one `ffmpeg -c copy` per camera
running, restarting it with backoff when the camera or the link drops.
No re-encoding, so a Raspberry-Pi-class box handles several cameras.

Env: OV_API (https://api.example.kz), OV_AGENT_TOKEN (ova_...).
Camera URLs and the token are credentials: never logged.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

log = logging.getLogger("edge-agent")

API = os.environ.get("OV_API", "").rstrip("/")
TOKEN = os.environ.get("OV_AGENT_TOKEN", "")
MAX_BACKOFF = 60


def fetch_config() -> dict:
    req = urllib.request.Request(f"{API}/api/v1/edge/config", headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def ffmpeg_cmd(source: str, publish: str) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-timeout", "10000000", "-i", source,
        "-map", "0:v:0", "-c", "copy", "-f", "mpegts", publish,
    ]  # fmt: skip


class Stream:
    def __init__(self, camera_id: int, source: str, publish: str):
        self.camera_id, self.source, self.publish = camera_id, source, publish
        self.proc: subprocess.Popen | None = None
        self.backoff = 1.0
        self.next_start = 0.0
        self.started_at = 0.0

    def tick(self) -> None:
        if self.proc and self.proc.poll() is None:
            if time.monotonic() - self.started_at > 60:
                self.backoff = 1.0  # stable for a minute
            return
        if self.proc:
            log.warning("camera %s: ffmpeg exited (%s), retry in %.0fs", self.camera_id, self.proc.returncode, self.backoff)
            self.proc = None
            self.next_start = time.monotonic() + self.backoff
            self.backoff = min(self.backoff * 2, MAX_BACKOFF)
        if time.monotonic() >= self.next_start:
            log.info("camera %s: starting", self.camera_id)
            self.proc = subprocess.Popen(ffmpeg_cmd(self.source, self.publish), stderr=subprocess.DEVNULL)
            self.started_at = time.monotonic()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not API or not TOKEN:
        sys.exit("OV_API and OV_AGENT_TOKEN are required")
    streams: dict[int, Stream] = {}
    running = True

    def _stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    poll, next_poll = 30, 0.0
    while running:
        if time.monotonic() >= next_poll:
            try:
                cfg = fetch_config()
                poll = cfg.get("poll_seconds", 30)
                wanted = {s["camera_id"]: s for s in cfg["streams"]}
                for cid in set(streams) - set(wanted):
                    log.info("camera %s: removed", cid)
                    streams.pop(cid).stop()
                for cid, s in wanted.items():
                    cur = streams.get(cid)
                    if cur and (cur.source, cur.publish) != (s["source_url"], s["publish_url"]):
                        cur.stop()
                        cur = None
                    if cur is None:
                        streams[cid] = Stream(cid, s["source_url"], s["publish_url"])
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    log.error("agent token rejected; was this agent deleted?")
                else:
                    log.warning("config fetch failed: HTTP %s", e.code)
            except Exception as e:
                log.warning("config fetch failed: %s", type(e).__name__)
            next_poll = time.monotonic() + poll
        for s in streams.values():
            s.tick()
        time.sleep(1)
    for s in streams.values():
        s.stop()


if __name__ == "__main__":
    main()
