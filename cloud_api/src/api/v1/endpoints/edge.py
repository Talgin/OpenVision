"""Edge agents: one small container on the client's LAN that reads their cameras
and pushes each one to our ingest over SRT. Outbound only, nothing exposed.
"""

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import Principal, client_ip, require, unauthorized
from src.core.config import get_settings
from src.core.security import decrypt, new_opaque_token, token_digest, utcnow
from src.db.session import get_session
from src.models import Camera, EdgeAgent
from src.schemas import (
    AgentToken,
    EdgeAgentCreate,
    EdgeAgentCreated,
    EdgeAgentOut,
    EdgeAgentUpdate,
    EdgeConfigOut,
    EdgeStreamConfig,
)
from src.services import audit, ingest
from src.services.tokens import as_utc

router = APIRouter()
agent_router = APIRouter()

POLL_SECONDS = 30
_bearer = HTTPBearer(auto_error=False)


def install_command(token: str) -> str:
    api = get_settings().public_base_url
    return (
        "docker run -d --name openvision-edge --restart unless-stopped --network host "
        f"-e OV_API={api} -e OV_AGENT_TOKEN={token} openvision/edge-agent:0.1.0"
    )


def _out(agent: EdgeAgent, cls=EdgeAgentOut, **extra):
    online = agent.last_seen_at is not None and as_utc(agent.last_seen_at) > utcnow() - timedelta(
        seconds=POLL_SECONDS * 3
    )
    return cls.model_validate({**{c: getattr(agent, c) for c in ("id", "name", "last_seen_at", "created_at")},
                               "online": online, **extra})  # fmt: skip


@router.get("", response_model=list[EdgeAgentOut])
async def list_agents(p: Principal = Depends(require("cameras.read"))):
    return [_out(a) for a in await p.scope.list(p.scope.select(EdgeAgent).order_by(EdgeAgent.created_at))]


@router.post("", response_model=EdgeAgentCreated, status_code=201)
async def create_agent(body: EdgeAgentCreate, request: Request, p: Principal = Depends(require("cameras.write"))):
    token = new_opaque_token("ova")
    agent = p.scope.add(EdgeAgent(name=body.name, token_digest=token_digest(token)))
    await p.scope.session.flush()
    audit.record(
        p.scope.session, "edge_agent.created", tenant_id=p.tenant.id, actor_user_id=p.user.id,
        target=f"edge_agent:{agent.id}", ip=client_ip(request),
    )  # fmt: skip
    await p.scope.commit()
    return _out(agent, EdgeAgentCreated, token=token, install_command=install_command(token))


@router.patch("/{agent_id}", response_model=EdgeAgentOut)
async def rename_agent(
    agent_id: uuid.UUID, body: EdgeAgentUpdate, request: Request, p: Principal = Depends(require("cameras.write"))
):
    agent = await p.scope.get(EdgeAgent, agent_id, "edge agent")
    agent.name = body.name
    await p.scope.commit()
    return _out(agent)


@router.post("/{agent_id}/rotate-token", response_model=AgentToken)
async def rotate_agent_token(
    agent_id: uuid.UUID, request: Request, p: Principal = Depends(require("cameras.write"))
):
    """New token for a lost or leaked one. The agent stops working until it is
    reinstalled with the new command; its cameras stay as they are."""
    agent = await p.scope.get(EdgeAgent, agent_id, "edge agent")
    token = new_opaque_token("ova")
    agent.token_digest = token_digest(token)
    agent.last_seen_at = None
    audit.record(
        p.scope.session, "edge_agent.token_rotated", tenant_id=p.tenant.id, actor_user_id=p.user.id,
        target=f"edge_agent:{agent.id}", ip=client_ip(request),
    )  # fmt: skip
    await p.scope.commit()
    return AgentToken(token=token, install_command=install_command(token))


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: uuid.UUID, request: Request, p: Principal = Depends(require("cameras.write"))):
    agent = await p.scope.get(EdgeAgent, agent_id, "edge agent")
    await p.scope.session.execute(
        update(Camera).where(Camera.tenant_id == p.tenant.id, Camera.edge_agent_id == agent.id).values(enabled=False)
    )
    await p.scope.session.delete(agent)
    audit.record(
        p.scope.session, "edge_agent.deleted", tenant_id=p.tenant.id, actor_user_id=p.user.id,
        target=f"edge_agent:{agent_id}", ip=client_ip(request),
    )  # fmt: skip
    await p.scope.commit()
    return Response(status_code=204)


async def agent_from_token(token: str, session: AsyncSession) -> EdgeAgent | None:
    return (
        await session.execute(select(EdgeAgent).where(EdgeAgent.token_digest == token_digest(token)))
    ).scalar_one_or_none()


@agent_router.get("/config", response_model=EdgeConfigOut, include_in_schema=False)
async def agent_config(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer), session: AsyncSession = Depends(get_session)
):
    """Called by the agent itself (Bearer = agent token), every poll_seconds."""
    agent = await agent_from_token(creds.credentials, session) if creds else None
    if agent is None:
        raise unauthorized("unknown agent token")
    agent.last_seen_at = utcnow()
    cams = (
        await session.execute(
            select(Camera).where(
                Camera.tenant_id == agent.tenant_id,
                Camera.edge_agent_id == agent.id,
                Camera.enabled.is_(True),
                Camera.deleted.is_(False),
            )
        )
    ).scalars()
    token = creds.credentials
    streams = [
        EdgeStreamConfig(
            camera_id=c.id,
            source_url=decrypt(c.source_url_enc),
            publish_url=ingest.publish_urls(c.id, ingest.EDGE_USER, token)[0],
        )
        for c in cams
        if c.source_url_enc
    ]
    await session.commit()
    return EdgeConfigOut(agent_id=agent.id, poll_seconds=POLL_SECONDS, streams=streams)
