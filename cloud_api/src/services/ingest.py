"""mediamtx: every camera, whatever its mode, becomes path cam/<id> on our ingest.

Pull cameras: mediamtx pulls the client URL (path source = that URL).
Push / edge cameras: path source = publisher; the publisher authenticates
through our HTTP auth hook (endpoints/ingest_hook.py).
Workers read rtsp://<internal user>@mediamtx:8554/cam/<id>.
"""

import logging
from urllib.parse import quote

import httpx

from src.core.config import get_settings
from src.models import Camera, SourceMode

logger = logging.getLogger("ingest")

PUSH_USER = "push"
EDGE_USER = "edge"


def path_name(camera_id: int) -> str:
    return f"cam/{camera_id}"


def internal_read_url(camera_id: int) -> str:
    s = get_settings()
    base = s.mediamtx_internal_rtsp.split("://", 1)[1]
    creds = f"{quote(s.ingest_internal_user, safe='')}:{quote(s.ingest_internal_password, safe='')}"
    return f"rtsp://{creds}@{base}/{path_name(camera_id)}"


def publish_urls(camera_id: int, user: str, secret: str) -> tuple[str, str]:
    """(srt_url, rtsp_url) a client or our edge agent publishes to."""
    s = get_settings()
    path = path_name(camera_id)
    srt = f"srt://{s.ingest_public_host}:{s.ingest_srt_port}?streamid=publish:{path}:{user}:{secret}&pkt_size=1316"
    rtsp = f"rtsp://{user}:{quote(secret, safe='')}@{s.ingest_public_host}:{s.ingest_rtsp_port}/{path}"
    return srt, rtsp


class MediaMTX:
    """Control API v3 client. Authenticates as the internal user (the hook allows it)."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        s = get_settings()
        self._client = client or httpx.AsyncClient(
            base_url=s.mediamtx_api_url, auth=(s.ingest_internal_user, s.ingest_internal_password), timeout=5
        )

    async def configure(self, camera: Camera, source_url: str | None) -> None:
        if camera.mode == SourceMode.PULL:
            conf = {"source": source_url, "sourceOnDemand": False, "rtspTransport": "tcp"}
        else:
            conf = {"source": "publisher"}
        name = path_name(camera.id)
        r = await self._client.post(f"/v3/config/paths/add/{name}", json=conf)
        if r.status_code == 400 and "already exists" in r.text:
            r = await self._client.patch(f"/v3/config/paths/patch/{name}", json=conf)
        r.raise_for_status()

    async def remove(self, camera_id: int) -> None:
        r = await self._client.delete(f"/v3/config/paths/delete/{path_name(camera_id)}")
        if r.status_code not in (200, 404) and "not found" not in r.text:
            r.raise_for_status()

    async def paths(self) -> dict[str, dict]:
        """name -> runtime state (ready, tracks, bytesReceived, ...)."""
        out: dict[str, dict] = {}
        page = 0
        while True:
            r = await self._client.get("/v3/paths/list", params={"page": page, "itemsPerPage": 500})
            r.raise_for_status()
            body = r.json()
            for item in body.get("items", []):
                out[item["name"]] = item
            page += 1
            if page >= body.get("pageCount", 1):
                return out

    async def aclose(self) -> None:
        await self._client.aclose()
