"""Demo data for client-app development: no GPU, no cameras, no mediamtx.

    python -m src.workers.demo seed       # demo account, users, cameras
    python -m src.workers.demo simulate   # fake trt-processor events every few seconds

`simulate` writes messages in trt-processor's exact output format to the
pipeline stream, so the bridge, the database, the REST API and the WebSocket
all run for real; only the video and the GPU are faked.
"""

import asyncio
import logging
import random
import subprocess
import sys
import uuid
import zlib
from datetime import timedelta
from pathlib import Path

import msgpack
import redis.asyncio as aioredis
from sqlalchemy import select

from src.core.config import get_settings
from src.core.security import encrypt, hash_password, new_opaque_token, redact_url, token_digest, utcnow
from src.db.session import SessionLocal
from src.models import Camera, CameraStatus, EdgeAgent, Plan, Role, SourceMode, Tenant, User

logger = logging.getLogger("demo")

DEMO_EMAIL = "demo@openvision.dev"
VIEWER_EMAIL = "viewer@openvision.dev"
DEMO_PASSWORD = "demo-password-1"  # dev only; seed refuses to run in prod
CROPS, FRAMES = 12, 4
KNOWN_PEOPLE = [str(uuid.UUID(int=i)) for i in range(1, 6)]


async def seed() -> None:
    if get_settings().environment == "prod":
        sys.exit("refusing to seed demo data in prod")
    async with SessionLocal() as session:
        if (await session.execute(select(User).where(User.email == DEMO_EMAIL))).scalar_one_or_none():
            print("demo account exists")
            return
        tenant = Tenant(name="Demo Retail LLP", plan=Plan.FACE, max_cameras=10, retention_days=30)
        session.add(tenant)
        await session.flush()
        for email, name, role in ((DEMO_EMAIL, "Demo Owner", Role.OWNER), (VIEWER_EMAIL, "Demo Viewer", Role.VIEWER)):
            session.add(User(tenant_id=tenant.id, email=email, full_name=name,
                             password_hash=hash_password(DEMO_PASSWORD), role=role))  # fmt: skip
        agent = EdgeAgent(tenant_id=tenant.id, name="Warehouse mini-PC", token_digest=token_digest(new_opaque_token("ova")))
        session.add(agent)
        await session.flush()
        pull_url = "rtsp://admin:secret@cam-entrance.example.com:554/Streaming/Channels/101"
        hd = {"codec": "h264", "width": 1920, "height": 1080, "fps": 25.0}
        session.add_all([
            Camera(tenant_id=tenant.id, name="Entrance", location="Main door", mode=SourceMode.PULL,
                   source_url_enc=encrypt(pull_url), source_url_redacted=redact_url(pull_url),
                   status=CameraStatus.ONLINE, stream_info=hd, last_seen_at=utcnow()),
            Camera(tenant_id=tenant.id, name="Checkout", location="Tills 1-4", mode=SourceMode.PUSH,
                   push_key_digest=token_digest(new_opaque_token("ovk")),
                   status=CameraStatus.ONLINE, stream_info={**hd, "codec": "hevc"}, last_seen_at=utcnow()),
            Camera(tenant_id=tenant.id, name="Loading bay", location="Warehouse", mode=SourceMode.EDGE,
                   edge_agent_id=agent.id, source_url_enc=encrypt("rtsp://192.168.1.64:554/stream1"),
                   source_url_redacted="rtsp://192.168.1.64:554/stream1",
                   status=CameraStatus.OFFLINE, status_reason="no_video", stream_info={**hd, "fps": 12.0},
                   last_seen_at=utcnow() - timedelta(hours=3)),
        ])  # fmt: skip
        await session.commit()
    print(f"demo account: {DEMO_EMAIL} / {DEMO_PASSWORD} (owner), {VIEWER_EMAIL} / {DEMO_PASSWORD} (viewer)")


def ensure_media(root: Path) -> None:
    """Placeholder crops and frames, made once with ffmpeg's test sources."""
    for bucket, n, size in (("face-crops-bucket/demo", CROPS, "160x160"), ("face-full-frames-bucket/demo", FRAMES, "1280x720")):
        d = root / bucket
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            f = d / f"{i}.jpg"
            if not f.exists():
                subprocess.run(
                    ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=1",
                     "-ss", str(i * 3), "-frames:v", "1", "-y", str(f)],
                    check=True,
                )  # fmt: skip


def fake_message(camera_id: int) -> dict:
    """Same keys as trt-processor's `message` in revised_bg_worker.process_event."""
    s = get_settings()
    known = random.random() < 0.35
    person = random.choice(KNOWN_PEOPLE) if known else str(uuid.uuid4())
    base = f"http://{s.ingest_public_host}:9000"
    x, y, w = random.randint(100, 1500), random.randint(80, 700), random.randint(60, 180)
    return {
        "event_id": str(uuid.uuid4()),
        "camera_host": "demo",
        "camera_id": camera_id,
        "crop_image_url": f"{base}/face-crops-bucket/demo/{random.randrange(CROPS)}.jpg",
        "datetime": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "face_id": person,
        "distance": round(random.uniform(0.6, 0.95), 3) if known else None,
        "face_image_url": f"{base}/database-photos-bucket/{person}/1.jpg" if known else None,
        "frame_image_url": f"{base}/face-full-frames-bucket/demo/{random.randrange(FRAMES)}.jpg",
        "recognition_confidence": round(random.uniform(0.6, 0.95), 3) if known else None,
        "x": x, "y": y, "w": w, "h": int(w * 1.25),
    }  # fmt: skip


async def simulate(interval: float = 4.0) -> None:
    s = get_settings()
    ensure_media(Path(s.media_local_dir))
    r = aioredis.from_url(s.pipeline_redis_url)
    logger.info("emitting a fake event every ~%ss to %s", interval, s.pipeline_output_stream)
    while True:
        async with SessionLocal() as session:
            ids = (
                await session.execute(
                    select(Camera.id).where(
                        Camera.enabled.is_(True), Camera.deleted.is_(False), Camera.status == CameraStatus.ONLINE
                    )
                )
            ).scalars().all()
        if ids:
            msg = fake_message(random.choice(ids))
            await r.xadd(s.pipeline_output_stream, {"data": zlib.compress(msgpack.packb(msg, use_bin_type=True))})
        await asyncio.sleep(random.uniform(interval * 0.5, interval * 1.5))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "seed":
        asyncio.run(seed())
    elif cmd == "simulate":
        asyncio.run(simulate())
    else:
        sys.exit("usage: python -m src.workers.demo seed|simulate")
