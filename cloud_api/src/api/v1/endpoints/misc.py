"""Devices (push tokens), the audit log, and our staff's GPU node registry."""

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import Principal, current, platform_staff, require
from src.core.errors import AppError, not_found
from src.db.session import get_session
from src.models import AuditLog, Camera, Device, GpuNode
from src.schemas import AuditOut, DeviceIn, DeviceOut, GpuNodeIn, GpuNodeOut, Page

devices_router = APIRouter()
audit_router = APIRouter()
platform_router = APIRouter(dependencies=[Depends(platform_staff)])


@devices_router.post("", response_model=DeviceOut, status_code=201)
async def register_device(body: DeviceIn, p: Principal = Depends(current)):
    """Idempotent per push token: re-registering moves the token to the current user."""
    session = p.scope.session
    await session.execute(delete(Device).where(Device.push_token == body.push_token))
    device = p.scope.add(Device(user_id=p.user.id, platform=body.platform, push_token=body.push_token))
    await p.scope.commit()
    return device


@devices_router.delete("/{device_id}", status_code=204)
async def unregister_device(device_id: uuid.UUID, p: Principal = Depends(current)):
    device = await p.scope.get(Device, device_id, "device")
    if device.user_id != p.user.id:
        raise not_found("device")
    await p.scope.session.delete(device)
    await p.scope.commit()
    return Response(status_code=204)


@audit_router.get("", response_model=Page[AuditOut])
async def list_audit(
    p: Principal = Depends(require("audit.read")),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
):
    """Newest first. Cursors are opaque strings, as for events."""
    q = p.scope.select(AuditLog)
    if cursor:
        if not cursor.isdigit():
            raise AppError(400, "invalid_cursor", "cursor is not valid")
        q = q.where(AuditLog.id < int(cursor))
    rows = await p.scope.list(q.order_by(AuditLog.id.desc()).limit(limit + 1))
    return Page[AuditOut](
        items=[AuditOut.model_validate(r) for r in rows[:limit]],
        next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None,
    )


async def _node_out(session: AsyncSession, node: GpuNode) -> GpuNodeOut:
    n = (await session.execute(select(func.count()).where(Camera.gpu_node_id == node.id))).scalar_one()
    return GpuNodeOut.model_validate(node).model_copy(update={"assigned": n})


@platform_router.get("/gpu-nodes", response_model=list[GpuNodeOut])
async def list_nodes(session: AsyncSession = Depends(get_session)):
    nodes = (await session.execute(select(GpuNode).order_by(GpuNode.name))).scalars().all()
    return [await _node_out(session, n) for n in nodes]


@platform_router.post("/gpu-nodes", response_model=GpuNodeOut, status_code=201)
async def add_node(body: GpuNodeIn, session: AsyncSession = Depends(get_session)):
    if (await session.execute(select(GpuNode).where(GpuNode.name == body.name))).scalar_one_or_none():
        raise AppError(409, "conflict", "a node with this name exists")
    node = GpuNode(**body.model_dump())
    session.add(node)
    await session.commit()
    return await _node_out(session, node)


@platform_router.patch("/gpu-nodes/{node_id}", response_model=GpuNodeOut)
async def update_node(node_id: uuid.UUID, body: GpuNodeIn, session: AsyncSession = Depends(get_session)):
    node = await session.get(GpuNode, node_id)
    if node is None:
        raise not_found("node")
    for k, v in body.model_dump().items():
        setattr(node, k, v)
    await session.commit()
    return await _node_out(session, node)
