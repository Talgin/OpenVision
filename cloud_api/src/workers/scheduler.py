"""Reconcile loop: camera registry -> mediamtx paths -> GPU node streams.

Every tick, idempotently:
  1. tear down deleted / disabled cameras (GPU stream, ingest path), purge deleted
  2. configure ingest paths for new or changed cameras
  3. check GPU node health; a node that fails node_fail_threshold checks in a
     row loses its cameras, which get placed elsewhere
  4. read mediamtx path state -> camera status and reason the client sees
  5. place cameras that have video on the node with most free capacity whose
     pipeline matches the tenant's plan (face pipeline never runs for a
     body-only tenant); remove streams a node runs that we did not assign
  6. publish camera_id -> tenant_id to the pipeline Redis, where trt-processor
     (MULTI_TENANT=true) reads it to keep face matching inside one tenant
  7. hourly: delete events past each tenant's retention

    python -m src.workers.scheduler
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import timedelta

import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.security import decrypt, utcnow
from src.db.session import SessionLocal
from src.models import Camera, CameraStatus, Device, EdgeAgent, Event, GpuNode, SourceMode, Tenant
from src.schemas import CameraOut
from src.services import ingest, live, probe
from src.services.ingest import MediaMTX
from src.services.streamer import Streamer, StreamerError
from src.services.tokens import as_utc

logger = logging.getLogger("scheduler")

# status_reason codes set here (probe.py has the source-side ones)
WAITING_FOR_VIDEO = "waiting_for_video"  # push / edge: nobody is publishing yet
NO_VIDEO = "no_video"  # was online, video stopped
EDGE_AGENT_OFFLINE = "edge_agent_offline"
WAITING_FOR_GPU = "waiting_for_gpu"  # video is fine, no capacity: our problem, not theirs
INGEST_ERROR = "ingest_error"

UNREACHABLE_AFTER = timedelta(seconds=60)  # pull: no video this long after configuring
RELEASE_GPU_AFTER = timedelta(minutes=10)  # offline this long: free the GPU slot
RETENTION_EVERY = 3600


def stream_cost(cam: Camera) -> int:
    """1080p-equivalent slots. WORKER_GPU_TUNING: 4K costs roughly double."""
    h = (cam.stream_info or {}).get("height") or 1080
    return 2 if h > 1080 else 1


TENANT_MAP_KEY = "openvision:camera_tenant"  # trt-processor TENANT_MAP_KEY


class Scheduler:
    def __init__(self, mtx: MediaMTX, streamer_for: Callable[[GpuNode], Streamer], pipeline_redis=None):
        self.mtx = mtx
        self.streamer_for = streamer_for
        self.pipeline_redis = pipeline_redis
        self.configured_at: dict[int, float] = {}
        self.last_retention = 0.0

    async def tick(self, session: AsyncSession) -> None:
        cams = list((await session.execute(select(Camera))).scalars())
        nodes = {n.id: n for n in (await session.execute(select(GpuNode))).scalars()}
        before = {c.id: (c.status, c.status_reason) for c in cams}

        await self._teardown(session, cams, nodes)
        cams = [c for c in cams if not c.deleted]
        await self._configure_ingest(cams)
        live_streams = await self._check_nodes(cams, nodes)
        await self._update_status(session, cams)
        await self._place(session, cams, nodes, live_streams)
        await session.commit()
        await self._publish_tenant_map(cams)

        for c in cams:
            if before.get(c.id) != (c.status, c.status_reason):
                await live.publish(c.tenant_id, "camera_status", CameraOut.model_validate(c).model_dump(mode="json"))
        if time.monotonic() - self.last_retention > RETENTION_EVERY:
            await self._retention(session)
            self.last_retention = time.monotonic()

    # 1 ---------------------------------------------------------------------
    async def _teardown(self, session, cams, nodes) -> None:
        for c in cams:
            if c.enabled and not c.deleted:
                continue
            if c.gpu_node_id and c.gpu_node_id in nodes:
                await self._unassign(c, nodes[c.gpu_node_id])
            if c.ingest_configured:
                try:
                    await self.mtx.remove(c.id)
                    c.ingest_configured = False
                except Exception:
                    logger.exception("camera %s: ingest remove failed", c.id)
                    continue
            if c.deleted and not c.gpu_node_id:
                # explicit, not only ON DELETE CASCADE: SQLite does not enforce it
                await session.execute(delete(Event).where(Event.camera_id == c.id))
                await session.delete(c)
                logger.info("camera %s purged", c.id)
        await session.flush()
        # deleted accounts go once their cameras are gone
        gone = (
            await session.execute(
                select(Tenant).where(
                    Tenant.deleted_at.is_not(None),
                    ~select(Camera.id).where(Camera.tenant_id == Tenant.id).exists(),
                )
            )
        ).scalars()
        for t in gone:
            for model in (Event, Device):  # users and their tokens went at deletion time
                await session.execute(delete(model).where(model.tenant_id == t.id))
            await session.delete(t)
            logger.info("tenant %s purged", t.id)

    # 2 ---------------------------------------------------------------------
    async def _configure_ingest(self, cams) -> None:
        for c in cams:
            if not c.enabled or c.ingest_configured:
                continue
            url = decrypt(c.source_url_enc) if c.mode == SourceMode.PULL and c.source_url_enc else None
            try:
                await self.mtx.configure(c, url)
            except Exception:
                logger.exception("camera %s: ingest configure failed", c.id)
                c.status, c.status_reason = CameraStatus.ERROR, INGEST_ERROR
                continue
            c.ingest_configured = True
            c.status, c.status_reason = CameraStatus.CONNECTING, None
            self.configured_at[c.id] = time.monotonic()

    # 3 ---------------------------------------------------------------------
    async def _check_nodes(self, cams, nodes) -> dict:
        threshold = get_settings().node_fail_threshold
        live_streams = {}
        for node in nodes.values():
            if not node.enabled:
                continue
            try:
                live_streams[node.id] = await self.streamer_for(node).stream_ids()
                node.healthy, node.consecutive_failures, node.last_ok_at = True, 0, utcnow()
            except Exception as e:
                node.consecutive_failures += 1
                logger.warning("node %s check failed (%s/%s): %s", node.name, node.consecutive_failures, threshold, e)
                if node.consecutive_failures >= threshold and node.healthy:
                    node.healthy = False
                    logger.error("node %s marked down, releasing its cameras", node.name)
                if not node.healthy:
                    for c in cams:
                        if c.gpu_node_id == node.id:
                            c.gpu_node_id = None
        return live_streams

    # 4 ---------------------------------------------------------------------
    async def _update_status(self, session, cams) -> None:
        try:
            paths = await self.mtx.paths()
        except Exception:
            logger.exception("mediamtx path list failed; statuses unchanged this tick")
            return
        agents = {a.id: a for a in (await session.execute(select(EdgeAgent))).scalars()}
        now = utcnow()
        for c in cams:
            if not c.enabled:
                c.status, c.status_reason = CameraStatus.DISABLED, None
                continue
            if not c.ingest_configured:
                continue
            state = paths.get(ingest.path_name(c.id)) or {}
            if state.get("ready"):
                c.last_seen_at = now
                if c.stream_info is None or c.status == CameraStatus.ERROR:
                    await self._probe_internal(c)
                if c.status == CameraStatus.ERROR:
                    continue
                c.status = CameraStatus.ONLINE if c.gpu_node_id else CameraStatus.CONNECTING
                c.status_reason = None if c.gpu_node_id else WAITING_FOR_GPU
                continue
            if c.status == CameraStatus.ONLINE or (c.status == CameraStatus.OFFLINE and c.last_seen_at):
                c.status, c.status_reason = CameraStatus.OFFLINE, NO_VIDEO
            elif c.mode == SourceMode.EDGE:
                agent = agents.get(c.edge_agent_id)
                seen = agent and agent.last_seen_at and as_utc(agent.last_seen_at) > now - timedelta(minutes=2)
                c.status, c.status_reason = CameraStatus.CONNECTING, WAITING_FOR_VIDEO if seen else EDGE_AGENT_OFFLINE
            elif c.mode == SourceMode.PUSH:
                c.status, c.status_reason = CameraStatus.CONNECTING, WAITING_FOR_VIDEO
            else:
                started = self.configured_at.get(c.id, time.monotonic())
                late = time.monotonic() - started > UNREACHABLE_AFTER.total_seconds()
                c.status = CameraStatus.ERROR if late else CameraStatus.CONNECTING
                c.status_reason = probe.UNREACHABLE if late else None

    async def _probe_internal(self, c: Camera) -> None:
        """First video from a push / edge camera: learn what it is, refuse what we cannot decode."""
        code, out, _ = await probe.run_ffprobe(ingest.internal_read_url(c.id), get_settings().probe_timeout_seconds)
        if code != 0:
            return
        try:
            result = probe.interpret(json.loads(out))
        except json.JSONDecodeError:
            return
        c.stream_info = result.stream.model_dump() if result.stream else c.stream_info
        if not result.ok:
            c.status, c.status_reason = CameraStatus.ERROR, result.reason
        elif c.status == CameraStatus.ERROR:
            c.status, c.status_reason = CameraStatus.CONNECTING, None

    # 5 ---------------------------------------------------------------------
    async def _place(self, session, cams, nodes, live_streams) -> None:
        tenants = {t.id: t for t in (await session.execute(select(Tenant))).scalars()}
        used = {nid: 0 for nid in nodes}
        for c in cams:
            if c.gpu_node_id in used:
                used[c.gpu_node_id] += stream_cost(c)
        now = utcnow()

        for c in cams:
            node = nodes.get(c.gpu_node_id) if c.gpu_node_id else None
            # release slots held by streams that have been dead a while
            if node and c.status == CameraStatus.OFFLINE and c.last_seen_at:
                if as_utc(c.last_seen_at) < now - RELEASE_GPU_AFTER:
                    await self._unassign(c, node)
                    used[node.id] -= stream_cost(c)
                continue
            # a node restart loses its streams: re-add ours
            if node and node.healthy and node.id in live_streams and str(c.id) not in live_streams[node.id]:
                if await self._add(c, node):
                    live_streams[node.id].add(str(c.id))
                continue
            if node or c.status_reason != WAITING_FOR_GPU:
                continue
            tenant = tenants.get(c.tenant_id)
            cost = stream_cost(c)
            candidates = [
                n for n in nodes.values()
                if n.enabled and n.healthy and n.id in live_streams
                and tenant and n.plan == tenant.plan and n.capacity - used[n.id] >= cost
            ]  # fmt: skip
            if not candidates:
                continue
            best = max(candidates, key=lambda n: n.capacity - used[n.id])
            if await self._add(c, best):
                c.gpu_node_id = best.id
                used[best.id] += cost
                live_streams[best.id].add(str(c.id))
                c.status, c.status_reason = CameraStatus.ONLINE, None

        assigned = {(c.gpu_node_id, str(c.id)) for c in cams if c.gpu_node_id}
        for nid, ids in live_streams.items():
            for sid in ids - {s for n, s in assigned if n == nid}:
                if sid.isdigit():
                    try:
                        await self.streamer_for(nodes[nid]).remove(int(sid), ingest.internal_read_url(int(sid)))
                        logger.info("node %s: removed orphan stream %s", nodes[nid].name, sid)
                    except Exception:
                        logger.exception("node %s: orphan %s remove failed", nodes[nid].name, sid)

    async def _add(self, c: Camera, node: GpuNode) -> bool:
        try:
            await self.streamer_for(node).add(c.id, c.name, ingest.internal_read_url(c.id), c.stream_info)
            logger.info("camera %s -> node %s", c.id, node.name)
            return True
        except Exception as e:
            logger.warning("camera %s: add to node %s failed: %s", c.id, node.name, e)
            return False

    async def _unassign(self, c: Camera, node: GpuNode) -> None:
        try:
            await self.streamer_for(node).remove(c.id, ingest.internal_read_url(c.id))
        except StreamerError as e:
            logger.warning("camera %s: remove from %s: %s", c.id, node.name, e)
        except Exception:
            logger.exception("camera %s: remove from %s failed", c.id, node.name)
            if node.healthy:
                return  # try again next tick
        c.gpu_node_id = None

    # 6 ---------------------------------------------------------------------
    async def _publish_tenant_map(self, cams) -> None:
        """Full sync each tick: set live cameras, drop deleted ones (processor then fails closed)."""
        if self.pipeline_redis is None:
            return
        wanted = {str(c.id): str(c.tenant_id) for c in cams}
        try:
            stale = [k for k in await self.pipeline_redis.hkeys(TENANT_MAP_KEY) if k.decode() not in wanted]
            if stale:
                await self.pipeline_redis.hdel(TENANT_MAP_KEY, *stale)
            if wanted:
                await self.pipeline_redis.hset(TENANT_MAP_KEY, mapping=wanted)
        except Exception:
            logger.exception("tenant map publish failed")

    # 7 ---------------------------------------------------------------------
    async def _retention(self, session) -> None:
        for t in (await session.execute(select(Tenant))).scalars():
            cutoff = utcnow() - timedelta(days=t.retention_days)
            await session.execute(delete(Event).where(Event.tenant_id == t.id, Event.occurred_at < cutoff))
        await session.commit()


async def run() -> None:
    s = get_settings()
    streamers: dict = {}

    def streamer_for(node: GpuNode) -> Streamer:
        if node.streamer_url not in streamers:
            streamers[node.streamer_url] = Streamer(node.streamer_url)
        return streamers[node.streamer_url]

    sched = Scheduler(MediaMTX(), streamer_for, aioredis.from_url(s.pipeline_redis_url))
    logger.info("scheduler running every %ss", s.scheduler_interval_seconds)
    while True:
        try:
            async with SessionLocal() as session:
                await sched.tick(session)
        except Exception:
            logger.exception("tick failed")
        await asyncio.sleep(s.scheduler_interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
