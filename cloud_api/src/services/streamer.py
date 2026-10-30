"""Client for trt-streamer-pyservicemaker's nvmultiurisrcbin REST API.

Payloads mirror trt-streamer-pyservicemaker/common/sources.py (api_add_source,
api_remove_source, api_get_stream_info), including its quirks: a 200 response
is not success unless `reason` says so, and remove needs the same camera_url.
"""

import time

import httpx


class StreamerError(Exception):
    pass


def _headers() -> dict:
    return {"source": "vst", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}


class Streamer:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=5)

    async def stream_ids(self) -> set[str]:
        r = await self._client.get(f"{self.base_url}/get-stream-info", timeout=3)
        if r.status_code != 200:
            raise StreamerError(f"get-stream-info HTTP {r.status_code}")
        body = r.json()
        if body.get("reason") != "GET_LIVE_STREAM_INFO_SUCCESS":
            raise StreamerError(f"get-stream-info reason={body.get('reason')!r}")
        items = (body.get("stream-info") or {}).get("stream-info") or []
        return {str(x["camera_id"]) for x in items if x.get("camera_id") is not None}

    async def add(self, camera_id: int, name: str, url: str, stream_info: dict | None) -> None:
        info = stream_info or {}
        payload = {
            "key": "sensor",
            "value": {
                "camera_id": str(camera_id),
                "camera_name": name,
                "camera_url": url,
                "change": "camera_add",
                "metadata": {
                    "resolution": f"{info.get('width') or 1920}x{info.get('height') or 1080}",
                    "codec": info.get("codec") or "h264",
                    "framerate": int(info.get("fps") or 25),
                },
            },
            "headers": _headers(),
        }
        r = await self._client.post(f"{self.base_url}/add", json=payload)
        if r.status_code != 200:
            raise StreamerError(f"add HTTP {r.status_code}")

    async def remove(self, camera_id: int, url: str) -> None:
        payload = {
            "key": "sensor",
            "value": {"camera_id": str(camera_id), "camera_url": url, "change": "camera_remove"},
            "headers": _headers(),
        }
        r = await self._client.post(f"{self.base_url}/remove", json=payload)
        if r.status_code != 200:
            raise StreamerError(f"remove HTTP {r.status_code}")
        # 200 with an empty reason happens when the stream is already dead; the
        # live list is the truth either way.
        if str(camera_id) in await self.stream_ids():
            raise StreamerError(f"camera {camera_id} still present after remove")

    async def aclose(self) -> None:
        await self._client.aclose()
