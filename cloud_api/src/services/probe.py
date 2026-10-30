"""Check a pull URL before saving it, and tell the client in plain codes what is wrong.

Two jobs: refuse URLs that would make our DC connect to itself (SSRF), and run
ffprobe so the UI can show codec / resolution / fps before the camera is saved.
"""

import asyncio
import ipaddress
import json
import logging
import socket
from urllib.parse import urlsplit

from src.core.config import get_settings
from src.schemas import ProbeOut, StreamInfo

logger = logging.getLogger("probe")

ALLOWED_SCHEMES = {"rtsp", "rtsps"}
SUPPORTED_CODECS = {"h264", "hevc"}  # what nvv4l2decoder takes in our pipeline
MIN_FPS = 10
MIN_HEIGHT = 480

# reason codes, shared with Camera.status_reason
INVALID_URL = "invalid_url"
PRIVATE_ADDRESS = "private_address_refused"
UNREACHABLE = "source_unreachable"
AUTH_FAILED = "source_auth_failed"
NOT_FOUND = "source_not_found"
NO_VIDEO = "no_video_track"
UNSUPPORTED_CODEC = "unsupported_codec"


def check_url_shape(url: str) -> str | None:
    """Returns a reason code, or None when the URL may be probed."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return INVALID_URL
    if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
        return INVALID_URL
    return None


async def check_public_address(host: str, port: int | None) -> str | None:
    if get_settings().allow_private_sources:
        return None
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port or 554, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return UNREACHABLE
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return PRIVATE_ADDRESS
    return None


def _classify_ffprobe_error(stderr: str) -> str:
    s = stderr.lower()
    if "401" in s or "unauthorized" in s:
        return AUTH_FAILED
    if "404" in s or "not found" in s:
        return NOT_FOUND
    return UNREACHABLE


def _parse_rate(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "0"):
        return None
    num, _, den = rate.partition("/")
    try:
        return round(float(num) / float(den or 1), 2)
    except (ValueError, ZeroDivisionError):
        return None


def interpret(ffprobe_json: dict) -> ProbeOut:
    video = next((st for st in ffprobe_json.get("streams", []) if st.get("codec_type") == "video"), None)
    if video is None:
        return ProbeOut(ok=False, reason=NO_VIDEO)
    info = StreamInfo(
        codec=video.get("codec_name"),
        width=video.get("width"),
        height=video.get("height"),
        fps=_parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate")),
    )
    if info.codec not in SUPPORTED_CODECS:
        return ProbeOut(ok=False, reason=UNSUPPORTED_CODEC, stream=info)
    warnings = []
    if info.fps is not None and info.fps < MIN_FPS:
        warnings.append("low_fps")
    if info.height is not None and info.height < MIN_HEIGHT:
        warnings.append("low_resolution")
    return ProbeOut(ok=True, stream=info, warnings=warnings)


async def run_ffprobe(url: str, timeout: float) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
            "-timeout", str(int(timeout * 1_000_000)),
            "-print_format", "json", "-show_streams", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
    except FileNotFoundError:
        logger.error("ffprobe is not installed; camera checks cannot run")
        return -1, "", "ffprobe missing"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout + 2)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "timed out"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def probe(url: str) -> ProbeOut:
    reason = check_url_shape(url)
    if reason:
        return ProbeOut(ok=False, reason=reason)
    parts = urlsplit(url)
    reason = await check_public_address(parts.hostname, parts.port)
    if reason:
        return ProbeOut(ok=False, reason=reason)
    code, out, err = await run_ffprobe(url, get_settings().probe_timeout_seconds)
    if code != 0:
        # stderr can echo the URL with its password: classify it, never return or log it
        return ProbeOut(ok=False, reason=_classify_ffprobe_error(err))
    try:
        return interpret(json.loads(out))
    except json.JSONDecodeError:
        return ProbeOut(ok=False, reason=UNREACHABLE)
