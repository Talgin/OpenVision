"""Bridge (trt-processor output -> events), event API, media, probe parsing."""

import json
import uuid
import zlib
from pathlib import Path

import msgpack
from sqlalchemy import select

from src.core.config import get_settings
from src.core.security import encode_media_token
from src.db.session import SessionLocal
from src.models import Event
from src.services import probe
from src.services.redis import get_redis
from src.workers import bridge
from src.workers.demo import fake_message
from tests.conftest import bearer, push_camera, register

# Shape copied from trt-processor revised_bg_worker.process_event `message`
PROCESSOR_MESSAGE = {
    "event_id": "6f1c0c2e-6a55-4b8e-9d1a-3a8f0b9e7c11",
    "camera_host": "10.1.10.89",
    "camera_id": None,  # filled per test
    "crop_image_url": "http://10.1.10.89:9000/face-crops-bucket/2026/10/29/abc.jpg",
    "datetime": "2026-10-29T08:15:30Z",
    "face_id": "0d6c2f1e-1111-2222-3333-444455556666",
    "distance": 0.87,
    "face_image_url": "http://10.1.10.89:9000/database-photos-bucket/p/e.jpg",
    "frame_image_url": "http://10.1.10.89:9000/face-full-frames-bucket/2026/10/29/abc.jpg",
    "recognition_confidence": 0.87,
    "h": 120, "w": 96, "x": 640, "y": 220,
}  # fmt: skip


async def _tenant(client, tokens) -> str:
    return (await client.get("/api/v1/auth/me", headers=bearer(tokens))).json()["tenant"]["id"]


def test_object_key():
    assert bridge.object_key("http://h:9000/face-crops-bucket/a/b.jpg") == "face-crops-bucket/a/b.jpg"
    assert bridge.object_key(None) is None


def test_wire_format_round_trip():
    raw = zlib.compress(msgpack.packb(PROCESSOR_MESSAGE, use_bin_type=True))
    assert bridge.decode(raw) == PROCESSOR_MESSAGE


async def test_bridge_stores_event_under_cameras_tenant_and_publishes(client):
    t = await register(client, "o@a.kz")
    cam = await push_camera(client, t)
    tenant_id = await _tenant(client, t)
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(f"live:{tenant_id}")
    await pubsub.get_message(timeout=1)  # subscribe confirmation

    async with SessionLocal() as s:
        ev = await bridge.handle(s, {**PROCESSOR_MESSAGE, "camera_id": cam["id"]})
    assert ev is not None and str(ev.tenant_id) == tenant_id
    assert ev.is_known and ev.bbox == [640, 220, 96, 120]
    assert ev.crop_key == "face-crops-bucket/2026/10/29/abc.jpg"

    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
    body = json.loads(msg["data"])
    assert body["type"] == "event" and body["data"]["camera_name"] == "Door"
    assert body["data"]["crop_url"].startswith("http://test/api/v1/media/")

    # redelivery is a no-op
    async with SessionLocal() as s:
        assert await bridge.handle(s, {**PROCESSOR_MESSAGE, "camera_id": cam["id"]}) is None
    r = await client.get("/api/v1/events", headers=bearer(t))
    assert len(r.json()["items"]) == 1
    cam_now = (await client.get(f"/api/v1/cameras/{cam['id']}", headers=bearer(t))).json()
    assert cam_now["last_event_at"].startswith("2026-10-29T08:15:30")


async def test_bridge_drops_events_for_unknown_cameras(client):
    async with SessionLocal() as s:
        assert await bridge.handle(s, {**PROCESSOR_MESSAGE, "camera_id": 999}) is None
        assert await bridge.handle(s, {**PROCESSOR_MESSAGE, "camera_id": "garbage"}) is None
        assert (await s.execute(select(Event))).first() is None


async def test_unknown_face_is_not_known(client):
    t = await register(client, "o@a.kz")
    cam = await push_camera(client, t)
    async with SessionLocal() as s:
        ev = await bridge.handle(s, {**PROCESSOR_MESSAGE, "camera_id": cam["id"], "face_image_url": None})
    assert ev.is_known is False


async def test_event_paging_and_filters(client):
    t = await register(client, "o@a.kz")
    c1, c2 = await push_camera(client, t, "one"), await push_camera(client, t, "two")
    async with SessionLocal() as s:
        for i in range(7):
            m = fake_message(c1["id"] if i % 2 else c2["id"])
            m["datetime"] = f"2026-10-29T08:00:{i:02d}Z"
            await bridge.handle(s, m)

    seen, cursor = [], None
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/api/v1/events", headers=bearer(t), params=params)).json()
        seen += [e["occurred_at"] for e in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 7 and seen == sorted(seen, reverse=True)

    only_one = (await client.get("/api/v1/events", headers=bearer(t), params={"camera_id": c1["id"]})).json()
    assert {e["camera_id"] for e in only_one["items"]} == {c1["id"]} and len(only_one["items"]) == 3
    r = await client.get("/api/v1/events", headers=bearer(t), params={"since": "2026-10-29T08:00:05Z"})
    assert len(r.json()["items"]) == 2
    assert (await client.get("/api/v1/events", headers=bearer(t), params={"cursor": "!!"})).status_code == 400

    stats = (
        await client.get(
            "/api/v1/stats/summary", headers=bearer(t),
            params={"since": "2026-10-29T00:00:00Z", "until": "2026-10-30T00:00:00Z"},
        )
    ).json()  # fmt: skip
    assert stats["total_events"] == 7
    assert {c["camera_name"]: c["events"] for c in stats["by_camera"]} == {"one": 3, "two": 4}
    assert stats["by_hour"] == [{"start": "2026-10-29T08:00:00Z", "events": 7}]


async def test_local_media_serving(client):
    t = await register(client, "o@a.kz")
    tenant_id = uuid.UUID(await _tenant(client, t))
    root = Path(get_settings().media_local_dir)
    (root / "face-crops-bucket").mkdir(parents=True, exist_ok=True)
    (root / "face-crops-bucket" / "x.jpg").write_bytes(b"\xff\xd8jpeg")
    (root.parent / "secret.txt").write_text("nope")

    ok = await client.get(f"/api/v1/media/{encode_media_token(tenant_id, 'face-crops-bucket/x.jpg', 60)}")
    assert ok.status_code == 200 and ok.content == b"\xff\xd8jpeg"
    trav = await client.get(f"/api/v1/media/{encode_media_token(tenant_id, '../secret.txt', 60)}")
    assert trav.status_code == 404
    expired = await client.get(f"/api/v1/media/{encode_media_token(tenant_id, 'face-crops-bucket/x.jpg', -1)}")
    assert expired.status_code == 403


def test_probe_interpret():
    good = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                         "avg_frame_rate": "25/1"}]}  # fmt: skip
    r = probe.interpret(good)
    assert r.ok and r.stream.fps == 25.0 and r.warnings == []

    slow = {"streams": [{"codec_type": "video", "codec_name": "hevc", "width": 640, "height": 360,
                         "avg_frame_rate": "0/0", "r_frame_rate": "15000/1001"}]}  # fmt: skip
    r = probe.interpret(slow)
    assert r.ok and r.stream.fps == 14.99 and r.warnings == ["low_resolution"]

    mjpeg = {"streams": [{"codec_type": "video", "codec_name": "mjpeg", "avg_frame_rate": "8/1"}]}
    assert probe.interpret(mjpeg).reason == "unsupported_codec"
    assert probe.interpret({"streams": [{"codec_type": "audio"}]}).reason == "no_video_track"


def test_ffprobe_errors_are_classified_without_echoing():
    assert probe._classify_ffprobe_error("method DESCRIBE failed: 401 Unauthorized") == "source_auth_failed"
    assert probe._classify_ffprobe_error("404 Stream Not Found") == "source_not_found"
    assert probe._classify_ffprobe_error("Connection timed out") == "source_unreachable"
