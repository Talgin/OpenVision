import uuid

import pytest
from sqlalchemy import select

from src.core.security import encrypt
from src.db.session import SessionLocal
from src.models import Camera, CameraStatus, GpuNode, Plan, SourceMode, Tenant
from src.services import probe
from src.services.streamer import StreamerError
from src.workers.scheduler import Scheduler


class FakeMTX:
    def __init__(self):
        self.conf: dict[str, str | None] = {}
        self.ready: set[str] = set()

    async def configure(self, cam, url):
        self.conf[f"cam/{cam.id}"] = url

    async def remove(self, camera_id):
        self.conf.pop(f"cam/{camera_id}", None)

    async def paths(self):
        return {name: {"name": name, "ready": name in self.ready} for name in self.conf}


class FakeStreamer:
    def __init__(self):
        self.streams: set[str] = set()
        self.down = False

    async def stream_ids(self):
        if self.down:
            raise StreamerError("unreachable")
        return set(self.streams)

    async def add(self, camera_id, name, url, info):
        assert url.endswith(f"/cam/{camera_id}") and "ovinternal:internal-pass@" in url
        self.streams.add(str(camera_id))

    async def remove(self, camera_id, url):
        self.streams.discard(str(camera_id))


@pytest.fixture
def world(monkeypatch):
    async def no_probe(url, timeout):
        return 1, "", ""

    monkeypatch.setattr(probe, "run_ffprobe", no_probe)
    mtx = FakeMTX()
    streamers: dict[str, FakeStreamer] = {}

    def streamer_for(node):
        return streamers.setdefault(node.name, FakeStreamer())

    return Scheduler(mtx, streamer_for), mtx, streamers


async def _setup(plan=Plan.FACE, nodes=(("gpu1", Plan.FACE, 2),), cams=2):
    async with SessionLocal() as s:
        t = Tenant(name="t", plan=plan, max_cameras=10, retention_days=30)
        s.add(t)
        for name, nplan, cap in nodes:
            s.add(GpuNode(name=name, streamer_url=f"http://{name}", plan=nplan, capacity=cap))
        await s.flush()
        for i in range(cams):
            s.add(Camera(tenant_id=t.id, name=f"c{i}", mode=SourceMode.PULL,
                         source_url_enc=encrypt(f"rtsp://cam{i}.example.com/s"),
                         stream_info={"codec": "h264", "width": 1920, "height": 1080, "fps": 25}))  # fmt: skip
        await s.commit()
        return t.id


async def _tick(sched):
    async with SessionLocal() as s:
        await sched.tick(s)


async def _cams() -> dict[str, Camera]:
    async with SessionLocal() as s:
        return {c.name: c for c in (await s.execute(select(Camera))).scalars()}


async def test_camera_goes_pending_connecting_online(world):
    sched, mtx, streamers = world
    await _setup(cams=1)
    await _tick(sched)
    c = (await _cams())["c0"]
    assert c.status == CameraStatus.CONNECTING and c.ingest_configured
    assert mtx.conf[f"cam/{c.id}"] == "rtsp://cam0.example.com/s"  # decrypted only for mediamtx

    mtx.ready.add(f"cam/{c.id}")
    await _tick(sched)
    c = (await _cams())["c0"]
    assert c.status == CameraStatus.ONLINE and c.gpu_node_id is not None
    assert streamers["gpu1"].streams == {str(c.id)}


async def test_capacity_is_respected_and_overflow_waits(world):
    sched, mtx, streamers = world
    await _setup(cams=3)
    await _tick(sched)
    for c in (await _cams()).values():
        mtx.ready.add(f"cam/{c.id}")
    await _tick(sched)
    statuses = sorted((c.status.value, c.status_reason) for c in (await _cams()).values())
    assert statuses == [("connecting", "waiting_for_gpu"), ("online", None), ("online", None)]
    assert len(streamers["gpu1"].streams) == 2


async def test_body_tenant_never_lands_on_face_node(world):
    sched, mtx, streamers = world
    await _setup(plan=Plan.BODY, nodes=(("facegpu", Plan.FACE, 8), ("bodygpu", Plan.BODY, 8)), cams=1)
    await _tick(sched)
    c = (await _cams())["c0"]
    mtx.ready.add(f"cam/{c.id}")
    await _tick(sched)
    assert streamers["bodygpu"].streams == {str(c.id)}
    assert streamers["facegpu"].streams == set()


async def test_dead_node_releases_cameras_to_another(world):
    sched, mtx, streamers = world
    await _setup(nodes=(("gpu1", Plan.FACE, 4),), cams=1)
    await _tick(sched)
    c = (await _cams())["c0"]
    mtx.ready.add(f"cam/{c.id}")
    await _tick(sched)
    async with SessionLocal() as s:
        s.add(GpuNode(name="gpu2", streamer_url="http://gpu2", plan=Plan.FACE, capacity=4))
        await s.commit()
    await _tick(sched)  # gpu2 registers as healthy
    streamers["gpu1"].down = True
    for _ in range(3):
        await _tick(sched)
    assert streamers["gpu2"].streams == {str(c.id)}
    assert (await _cams())["c0"].status == CameraStatus.ONLINE


async def test_node_restart_gets_streams_back(world):
    sched, mtx, streamers = world
    await _setup(cams=1)
    await _tick(sched)
    c = (await _cams())["c0"]
    mtx.ready.add(f"cam/{c.id}")
    await _tick(sched)
    streamers["gpu1"].streams.clear()  # DeepStream restarted
    await _tick(sched)
    assert streamers["gpu1"].streams == {str(c.id)}


async def test_video_loss_is_offline_with_reason(world):
    sched, mtx, _ = world
    await _setup(cams=1)
    await _tick(sched)
    c = (await _cams())["c0"]
    mtx.ready.add(f"cam/{c.id}")
    await _tick(sched)
    mtx.ready.clear()
    await _tick(sched)
    c = (await _cams())["c0"]
    assert (c.status, c.status_reason) == (CameraStatus.OFFLINE, "no_video")


async def test_disable_and_delete_tear_down(world):
    sched, mtx, streamers = world
    await _setup(cams=2)
    await _tick(sched)
    cams = await _cams()
    for c in cams.values():
        mtx.ready.add(f"cam/{c.id}")
    await _tick(sched)
    async with SessionLocal() as s:
        a = await s.get(Camera, cams["c0"].id)
        b = await s.get(Camera, cams["c1"].id)
        a.enabled = False
        b.deleted, b.enabled = True, False
        await s.commit()
    await _tick(sched)
    assert streamers["gpu1"].streams == set()
    assert mtx.conf == {}
    left = await _cams()
    assert set(left) == {"c0"} and left["c0"].status == CameraStatus.DISABLED


async def test_orphan_streams_are_removed(world):
    sched, _, streamers = world
    await _setup(cams=0)
    await _tick(sched)
    streamers["gpu1"].streams.add("4242")
    await _tick(sched)
    assert streamers["gpu1"].streams == set()


async def test_push_camera_waits_for_video(world):
    sched, _, _ = world
    tid = await _setup(cams=0)
    async with SessionLocal() as s:
        s.add(Camera(tenant_id=tid, name="push", mode=SourceMode.PUSH, push_key_digest=uuid.uuid4().hex))
        await s.commit()
    await _tick(sched)
    await _tick(sched)
    c = (await _cams())["push"]
    assert (c.status, c.status_reason) == (CameraStatus.CONNECTING, "waiting_for_video")


async def test_tenant_map_follows_cameras(monkeypatch):
    import fakeredis

    async def no_probe(url, timeout):
        return 1, "", ""

    monkeypatch.setattr(probe, "run_ffprobe", no_probe)
    r = fakeredis.FakeAsyncRedis()
    await r.hset("openvision:camera_tenant", "999", "stale-camera")
    sched = Scheduler(FakeMTX(), lambda n: FakeStreamer(), r)
    tid = await _setup(cams=2)
    await _tick(sched)
    cams = await _cams()
    mapping = {k.decode(): v.decode() for k, v in (await r.hgetall("openvision:camera_tenant")).items()}
    assert mapping == {str(c.id): str(tid) for c in cams.values()}

    async with SessionLocal() as s:
        c = await s.get(Camera, cams["c0"].id)
        c.deleted, c.enabled = True, False
        await s.commit()
    await _tick(sched)
    assert set((await r.hgetall("openvision:camera_tenant"))) == {str(cams["c1"].id).encode()}
