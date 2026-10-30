"""mediamtx authMethod=http hook: mediamtx asks us before every publish / read / API call.

Allowed:
  publish cam/<id>  push camera, user "push", password = its stream key
  publish cam/<id>  edge camera, user "edge", password = its agent's token
  read, playback, api, metrics: the internal user (workers, scheduler)
Everything else is refused. Not exposed publicly; mediamtx adds ?secret=.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.endpoints.cameras import push_key_valid
from src.api.v1.endpoints.edge import agent_from_token
from src.core.config import get_settings
from src.db.session import get_session
from src.models import Camera, SourceMode, Tenant
from src.services import ingest

router = APIRouter()
logger = logging.getLogger("ingest.auth")

INTERNAL_ACTIONS = {"read", "playback", "api", "metrics"}


class HookIn(BaseModel):
    user: str = ""
    password: str = ""
    ip: str = ""
    action: str
    path: str = ""
    protocol: str = ""
    id: str | None = None
    query: str = ""


def _deny(reason: str, body: HookIn) -> Response:
    # no password, no query (it can carry credentials)
    logger.info("deny action=%s path=%s proto=%s ip=%s reason=%s", body.action, body.path, body.protocol, body.ip, reason)
    return Response(status_code=401)


def _internal(body: HookIn) -> bool:
    s = get_settings()
    return bool(s.ingest_internal_password) and (
        hmac.compare_digest(body.user, s.ingest_internal_user)
        and hmac.compare_digest(body.password, s.ingest_internal_password)
    )


def _camera_id(path: str) -> int | None:
    prefix, _, rest = path.partition("/")
    if prefix != "cam" or not rest.isdigit():
        return None
    return int(rest)


@router.post("/auth")
async def mediamtx_auth(body: HookIn, secret: str = Query(default=""), session: AsyncSession = Depends(get_session)):
    expected = get_settings().ingest_hook_secret
    if not expected or not hmac.compare_digest(secret, expected):
        return Response(status_code=403)

    if body.action in INTERNAL_ACTIONS:
        return Response(status_code=200) if _internal(body) else _deny("not internal", body)
    if body.action != "publish":
        return _deny("action", body)

    camera_id = _camera_id(body.path)
    if camera_id is None:
        return _deny("path", body)
    cam = await session.get(Camera, camera_id)
    if cam is None or cam.deleted or not cam.enabled:
        return _deny("camera", body)
    tenant = await session.get(Tenant, cam.tenant_id)
    if tenant is None or not tenant.is_active:
        return _deny("tenant", body)

    if cam.mode == SourceMode.PUSH and body.user == ingest.PUSH_USER and push_key_valid(cam, body.password):
        return Response(status_code=200)
    if cam.mode == SourceMode.EDGE and body.user == ingest.EDGE_USER and cam.edge_agent_id:
        agent = await agent_from_token(body.password, session)
        if agent is not None and agent.id == cam.edge_agent_id:
            return Response(status_code=200)
    return _deny("credentials", body)
