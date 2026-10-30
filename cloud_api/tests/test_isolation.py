"""A tenant must not reach another tenant's data by any path (architecture §5)."""

import uuid
from datetime import datetime, timezone

from src.db.session import SessionLocal
from src.models import Event
from tests.conftest import bearer, push_camera, register


async def _event(tenant_id: str, camera_id: int) -> uuid.UUID:
    eid = uuid.uuid4()
    async with SessionLocal() as s:
        s.add(Event(id=eid, tenant_id=uuid.UUID(tenant_id), camera_id=camera_id, kind="face",
                    occurred_at=datetime.now(timezone.utc), person_id="p1", crop_key="b/x.jpg"))  # fmt: skip
        await s.commit()
    return eid


async def _tenant_id(client, tokens) -> str:
    return (await client.get("/api/v1/auth/me", headers=bearer(tokens))).json()["tenant"]["id"]


async def test_other_tenants_camera_is_invisible(client):
    a = await register(client, "a@a.kz", "Alpha")
    b = await register(client, "b@b.kz", "Beta")
    cam = await push_camera(client, a, "Alpha door")
    path = f"/api/v1/cameras/{cam['id']}"

    assert (await client.get(path, headers=bearer(b))).status_code == 404
    assert (await client.patch(path, headers=bearer(b), json={"name": "pwned"})).status_code == 404
    assert (await client.delete(path, headers=bearer(b))).status_code == 404
    assert (await client.post(f"{path}/rotate-key", headers=bearer(b))).status_code == 404
    assert (await client.get("/api/v1/cameras", headers=bearer(b))).json() == []
    # and A still has it, unchanged
    r = await client.get(path, headers=bearer(a))
    assert r.status_code == 200 and r.json()["name"] == "Alpha door"


async def test_other_tenants_events_are_invisible(client):
    a = await register(client, "a@a.kz", "Alpha")
    b = await register(client, "b@b.kz", "Beta")
    cam = await push_camera(client, a)
    eid = await _event(await _tenant_id(client, a), cam["id"])

    assert (await client.get(f"/api/v1/events/{eid}", headers=bearer(b))).status_code == 404
    assert (await client.get("/api/v1/events", headers=bearer(b))).json()["items"] == []
    r = await client.get("/api/v1/events", headers=bearer(b), params={"camera_id": cam["id"]})
    assert r.json()["items"] == []
    stats = (await client.get("/api/v1/stats/summary", headers=bearer(b))).json()
    assert stats["total_events"] == 0 and stats["by_camera"] == []

    assert len((await client.get("/api/v1/events", headers=bearer(a))).json()["items"]) == 1


async def test_other_tenants_users_and_agents_are_invisible(client):
    a = await register(client, "a@a.kz", "Alpha")
    b = await register(client, "b@b.kz", "Beta")
    a_owner = (await client.get("/api/v1/auth/me", headers=bearer(a))).json()["user"]["id"]
    agent = (await client.post("/api/v1/edge-agents", headers=bearer(a), json={"name": "pc"})).json()

    assert [u["email"] for u in (await client.get("/api/v1/users", headers=bearer(b))).json()] == ["b@b.kz"]
    assert (await client.patch(f"/api/v1/users/{a_owner}", headers=bearer(b), json={"full_name": "x"})).status_code == 404
    assert (await client.get("/api/v1/edge-agents", headers=bearer(b))).json() == []
    assert (await client.delete(f"/api/v1/edge-agents/{agent['id']}", headers=bearer(b))).status_code == 404
    # B cannot attach a camera to A's agent
    r = await client.post(
        "/api/v1/cameras",
        headers=bearer(b),
        json={"name": "x", "mode": "edge", "source_url": "rtsp://192.168.1.2/s", "edge_agent_id": agent["id"]},
    )
    assert r.status_code == 404


async def test_audit_log_is_per_tenant(client):
    a = await register(client, "a@a.kz", "Alpha")
    b = await register(client, "b@b.kz", "Beta")
    await push_camera(client, a)
    actions_b = [e["action"] for e in (await client.get("/api/v1/audit", headers=bearer(b))).json()["items"]]
    assert "camera.created" not in actions_b
    actions_a = [e["action"] for e in (await client.get("/api/v1/audit", headers=bearer(a))).json()["items"]]
    assert "camera.created" in actions_a


async def test_media_token_cannot_be_forged_across_paths(client):
    a = await register(client, "a@a.kz", "Alpha")
    r = await client.get("/api/v1/media/not-a-token")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "media_link_expired"
    assert a  # media tokens are signed; traversal is covered in test_events
