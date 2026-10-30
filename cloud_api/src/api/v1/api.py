from fastapi import APIRouter

from src.api.v1.endpoints import auth, cameras, edge, events, ingest_hook, live, misc, users

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(cameras.router, prefix="/cameras", tags=["cameras"])
api_router.include_router(edge.router, prefix="/edge-agents", tags=["edge agents"])
api_router.include_router(events.router, prefix="/events", tags=["events"])
api_router.include_router(events.stats_router, prefix="/stats", tags=["events"])
api_router.include_router(events.media_router, prefix="/media", tags=["events"])
api_router.include_router(misc.devices_router, prefix="/devices", tags=["devices"])
api_router.include_router(misc.audit_router, prefix="/audit", tags=["audit"])
api_router.include_router(live.router, tags=["live"])
# Called by edge agents over the internet (Bearer = agent token); hidden from the schema
api_router.include_router(edge.agent_router, prefix="/edge")

# Not for the client apps, never exposed publicly: the proxy refuses /internal/*
internal_router = APIRouter(include_in_schema=False)
internal_router.include_router(ingest_hook.router, prefix="/ingest", tags=["internal"])
internal_router.include_router(misc.platform_router, prefix="/platform", tags=["platform staff"])
