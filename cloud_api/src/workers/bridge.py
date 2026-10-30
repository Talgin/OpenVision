"""trt-processor output stream -> tenant-scoped events + live fan-out.

trt-processor pushes zlib(msgpack(message)) to OUTPUT_REDIS_STREAM_KEY
(`filtered_events`) after face matching; trt-broker reads the same stream
with its own group, so adding ours does not take events from it.

The camera id is the only link to a tenant, which is why camera ids are
global (models.Camera.id). Events for unknown cameras are dropped.

    python -m src.workers.bridge
"""

import asyncio
import logging
import uuid
import zlib
from datetime import datetime, timezone
from urllib.parse import urlsplit

import msgpack
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.endpoints.events import to_out
from src.core.config import get_settings
from src.db.session import SessionLocal
from src.models import Camera, Event
from src.services import live

logger = logging.getLogger("bridge")
CONSUMER = "bridge-1"


def object_key(url: str | None) -> str | None:
    """'http://10.1.10.89:9000/face-crops-bucket/a/b.jpg' -> 'face-crops-bucket/a/b.jpg'."""
    if not url:
        return None
    path = urlsplit(url).path.lstrip("/")
    return path or None


def decode(raw: bytes) -> dict:
    return msgpack.unpackb(zlib.decompress(raw), raw=False)


def to_event(msg: dict, tenant_id: uuid.UUID) -> Event:
    bbox = [msg.get(k) for k in ("x", "y", "w", "h")]
    occurred = datetime.strptime(msg["datetime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return Event(
        id=uuid.UUID(msg["event_id"]),
        tenant_id=tenant_id,
        camera_id=int(msg["camera_id"]),
        kind=msg.get("kind", "face"),
        occurred_at=occurred,
        person_id=msg.get("face_id"),
        # trt-processor sets face_image_url only on a match in the known-faces
        # collection; unknown-collection matches keep it None.
        is_known=msg.get("face_image_url") is not None,
        confidence=msg.get("recognition_confidence"),
        bbox=bbox if all(v is not None for v in bbox) else None,
        crop_key=object_key(msg.get("crop_image_url")),
        frame_key=object_key(msg.get("frame_image_url")),
        attributes=msg.get("attributes"),
    )


async def handle(session: AsyncSession, msg: dict) -> Event | None:
    try:
        camera_id = int(msg["camera_id"])
    except (KeyError, TypeError, ValueError):
        logger.warning("message without a usable camera_id dropped")
        return None
    cam = await session.get(Camera, camera_id)
    if cam is None or cam.deleted:
        logger.debug("event for unknown camera %s dropped", camera_id)
        return None
    event = to_event(msg, cam.tenant_id)
    if await session.get(Event, event.id) is not None:
        return None  # redelivery after a crash before XACK
    session.add(event)
    cam.last_event_at = event.occurred_at
    await session.commit()
    names = {cam.id: cam.name}
    await live.publish(cam.tenant_id, "event", to_out(event, names).model_dump(mode="json"))
    return event


async def run() -> None:
    s = get_settings()
    r = aioredis.from_url(s.pipeline_redis_url)
    try:
        await r.xgroup_create(s.pipeline_output_stream, s.pipeline_consumer_group, id="$", mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
    logger.info("reading %s as group %s", s.pipeline_output_stream, s.pipeline_consumer_group)
    # "0" first: anything delivered to us before a restart and never acked
    start = "0"
    while True:
        try:
            batches = await r.xreadgroup(
                s.pipeline_consumer_group, CONSUMER, {s.pipeline_output_stream: start}, count=100, block=5000
            )
        except aioredis.ConnectionError:
            logger.warning("pipeline redis unavailable, retrying")
            await asyncio.sleep(2)
            continue
        if start == "0" and not any(msgs for _, msgs in batches):
            start = ">"
            continue
        for _, messages in batches:
            for entry_id, fields in messages:
                try:
                    async with SessionLocal() as session:
                        await handle(session, decode(fields[b"data"]))
                except Exception:
                    logger.exception("event %s failed, acking to avoid a poison loop", entry_id)
                await r.xack(s.pipeline_output_stream, s.pipeline_consumer_group, entry_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
