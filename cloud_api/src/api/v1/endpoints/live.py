"""WebSocket /api/v1/live: events and camera state as they happen.

Protocol (JSON text frames):
  client -> {"type": "auth", "token": "<access token>"}      first frame, within AUTH_TIMEOUT
  server -> {"type": "ready", "data": {"expires_at": ...}}
  server -> {"type": "event" | "camera_status" | "camera_deleted", "data": {...}}
  server -> {"type": "ping"}                                   every PING_SECONDS
  client -> {"type": "subscribe", "camera_ids": [1, 2] | null}  optional event filter
  client -> {"type": "auth", "token": ...}                     any time, to extend the session
The server closes with 4401 when the token is invalid or expires without a new
auth frame, and 4403 when the role cannot read events.
The token travels in a frame, not the URL, so it stays out of proxy logs.
"""

import asyncio
import json
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.api.deps import principal_from_token
from src.core.errors import AppError
from src.core.security import ACCESS, decode_token
from src.db.session import SessionLocal
from src.services import live
from src.services.redis import get_redis

router = APIRouter()

AUTH_TIMEOUT = 10
PING_SECONDS = 25
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403


async def _authenticate(token: str):
    async with SessionLocal() as session:
        p = await principal_from_token(token, session)
    return p, decode_token(token, ACCESS)["exp"]


@router.websocket("/live")
async def live_ws(ws: WebSocket):
    await ws.accept()
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT))
        if first.get("type") != "auth":
            raise ValueError
        p, expires = await _authenticate(str(first.get("token", "")))
    except (asyncio.TimeoutError, ValueError, AppError):
        await ws.close(CLOSE_UNAUTHORIZED)
        return
    except WebSocketDisconnect:
        return
    if "events.read" not in p.permissions:
        await ws.close(CLOSE_FORBIDDEN)
        return

    tenant_id = p.tenant.id
    state = {"expires": expires, "cameras": None}
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(live.channel(tenant_id))
    await ws.send_json({"type": "ready", "data": {"expires_at": expires}})

    async def from_client():
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "subscribe":
                ids = msg.get("camera_ids")
                state["cameras"] = {int(i) for i in ids} if ids else None
            elif msg.get("type") == "auth":
                try:
                    p2, exp = await _authenticate(str(msg.get("token", "")))
                except AppError:
                    await ws.close(CLOSE_UNAUTHORIZED)
                    return
                if p2.tenant.id != tenant_id:
                    await ws.close(CLOSE_UNAUTHORIZED)
                    return
                state["expires"] = exp
                await ws.send_json({"type": "ready", "data": {"expires_at": exp}})

    async def to_client():
        last_ping = time.monotonic()
        while True:
            if time.time() >= state["expires"]:
                await ws.close(CLOSE_UNAUTHORIZED)
                return
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is not None:
                body = json.loads(msg["data"])
                cams = state["cameras"]
                if body["type"] == "event" and cams is not None and body["data"].get("camera_id") not in cams:
                    continue
                await ws.send_text(msg["data"] if isinstance(msg["data"], str) else msg["data"].decode())
            if time.monotonic() - last_ping >= PING_SECONDS:
                await ws.send_json({"type": "ping"})
                last_ping = time.monotonic()

    tasks = [asyncio.create_task(from_client()), asyncio.create_task(to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await pubsub.unsubscribe()
        await pubsub.aclose()
