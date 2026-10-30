"""Points raised by the mobile developer (docs/from_mobile.md), one test each or so."""

from sqlalchemy import select

from src.db.session import SessionLocal
from src.models import Camera, Event, Tenant, User
from src.workers import bridge
from src.workers.demo import fake_message
from src.workers.scheduler import Scheduler
from tests.conftest import PASSWORD, add_user, bearer, push_camera, register
from tests.test_scheduler import FakeMTX, FakeStreamer


# 2. account deletion ---------------------------------------------------------

async def test_member_deletes_only_themselves(client):
    owner = await register(client, "o@a.kz")
    viewer = await add_user(client, owner, "v@a.kz", "viewer")
    r = await client.post("/api/v1/auth/delete-account", headers=bearer(viewer), json={"password": "wrong"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "wrong_password"
    r = await client.post("/api/v1/auth/delete-account", headers=bearer(viewer), json={"password": PASSWORD})
    assert r.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=bearer(viewer))).status_code == 401
    emails = [u["email"] for u in (await client.get("/api/v1/users", headers=bearer(owner))).json()]
    assert emails == ["o@a.kz"]


async def test_owner_deletes_the_company_and_everything_is_purged(client):
    owner = await register(client, "o@a.kz", "Gone LLP")
    other = await register(client, "x@b.kz", "Stays LLP")
    await add_user(client, owner, "v@a.kz", "viewer")
    cam = await push_camera(client, owner)
    keep = await push_camera(client, other)
    async with SessionLocal() as s:
        await bridge.handle(s, fake_message(cam["id"]))
        await bridge.handle(s, fake_message(keep["id"]))

    r = await client.post("/api/v1/auth/delete-account", headers=bearer(owner), json={"password": PASSWORD})
    assert r.status_code == 204
    # immediately: nobody signs in, emails are free again
    assert (await client.get("/api/v1/auth/me", headers=bearer(owner))).status_code == 401
    r = await client.post("/api/v1/auth/login", json={"email": "v@a.kz", "password": PASSWORD})
    assert r.status_code == 401
    assert (await register(client, "o@a.kz", "New LLP"))["access_token"]
    # the ingest refuses the old push key
    r = await client.post(
        "/internal/ingest/auth?secret=hook-secret",
        json={"action": "publish", "path": f"cam/{cam['id']}", "user": "push", "password": cam["push"]["stream_key"]},
    )
    assert r.status_code == 401

    # the scheduler purges cameras, events, then the tenant
    sched = Scheduler(FakeMTX(), lambda n: FakeStreamer())
    for _ in range(2):
        async with SessionLocal() as s:
            await sched.tick(s)
    async with SessionLocal() as s:
        names = {t.name for t in (await s.execute(select(Tenant))).scalars()}
        cams = {c.id for c in (await s.execute(select(Camera))).scalars()}
        events = {e.camera_id for e in (await s.execute(select(Event))).scalars()}
        users = {u.email for u in (await s.execute(select(User))).scalars()}
    assert names == {"Stays LLP", "New LLP"}
    assert cams == {keep["id"]} and events == {keep["id"]}
    assert users == {"x@b.kz", "o@a.kz"}


# 4. admin resets a team member's password -------------------------------------

async def test_admin_sets_password_and_old_sessions_end(client):
    owner = await register(client, "o@a.kz")
    op = await add_user(client, owner, "op@a.kz", "operator")
    op_id = (await client.get("/api/v1/auth/me", headers=bearer(op))).json()["user"]["id"]
    r = await client.post(f"/api/v1/users/{op_id}/password", headers=bearer(op), json={"new_password": "x" * 12})
    assert r.status_code == 403  # operators cannot
    r = await client.post(f"/api/v1/users/{op_id}/password", headers=bearer(owner), json={"new_password": "brand-new-pass"})
    assert r.status_code == 204
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": op["refresh_token"]})).status_code == 401
    r = await client.post("/api/v1/auth/login", json={"email": "op@a.kz", "password": "brand-new-pass"})
    assert r.status_code == 200
    me = (await client.get("/api/v1/auth/me", headers=bearer(owner))).json()["user"]["id"]
    r = await client.post(f"/api/v1/users/{me}/password", headers=bearer(owner), json={"new_password": "x" * 12})
    assert r.json()["error"]["code"] == "owner_protected"


# 5. edge agent: new token, rename ---------------------------------------------

async def test_agent_token_rotation_keeps_cameras(client):
    t = await register(client, "o@a.kz")
    agent = (await client.post("/api/v1/edge-agents", headers=bearer(t), json={"name": "pc"})).json()
    cam = (
        await client.post(
            "/api/v1/cameras", headers=bearer(t),
            json={"name": "lan", "mode": "edge", "source_url": "rtsp://192.168.1.5/s", "edge_agent_id": agent["id"]},
        )
    ).json()  # fmt: skip
    r = await client.post(f"/api/v1/edge-agents/{agent['id']}/rotate-token", headers=bearer(t))
    assert r.status_code == 200
    new = r.json()
    assert new["token"] != agent["token"] and new["token"] in new["install_command"]

    old_cfg = await client.get("/api/v1/edge/config", headers={"Authorization": f"Bearer {agent['token']}"})
    assert old_cfg.status_code == 401
    cfg = (await client.get("/api/v1/edge/config", headers={"Authorization": f"Bearer {new['token']}"})).json()
    assert [s["camera_id"] for s in cfg["streams"]] == [cam["id"]]
    assert (await client.get(f"/api/v1/cameras/{cam['id']}", headers=bearer(t))).json()["enabled"] is True

    r = await client.patch(f"/api/v1/edge-agents/{agent['id']}", headers=bearer(t), json={"name": "Warehouse"})
    assert r.status_code == 200 and r.json()["name"] == "Warehouse"


# 7. PATCH and null ----------------------------------------------------------

async def test_patch_null_clears_location_and_rejects_null_elsewhere(client):
    t = await register(client, "o@a.kz")
    cam = await push_camera(client, t)
    path = f"/api/v1/cameras/{cam['id']}"
    assert (await client.patch(path, headers=bearer(t), json={"location": "Gate"})).json()["location"] == "Gate"
    body = (await client.patch(path, headers=bearer(t), json={"location": None})).json()
    assert body["location"] is None and body["name"] == "Door"
    for field in ("name", "enabled", "source_url"):
        r = await client.patch(path, headers=bearer(t), json={field: None})
        assert r.status_code == 422, field  # was a 500 for name
        assert r.json()["error"]["details"][0]["field"] == field
    owner_id = (await client.get("/api/v1/auth/me", headers=bearer(t))).json()["user"]["id"]
    r = await client.patch(f"/api/v1/users/{owner_id}", headers=bearer(t), json={"full_name": None})
    assert r.status_code == 422


# 8-10. the published schema ------------------------------------------------

async def test_openapi_describes_real_errors_and_hides_internal(client):
    spec = (await client.get("/api/openapi.json")).json()
    assert not [p for p in spec["paths"] if p.startswith("/internal") or p.startswith("/api/v1/edge/")]
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    login = spec["paths"]["/api/v1/auth/login"]["post"]["responses"]
    for code in ("401", "422", "429"):
        assert login[code]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorOut")
    audit_params = {p["name"]: p for p in spec["paths"]["/api/v1/audit"]["get"]["parameters"]}
    assert audit_params["cursor"]["schema"]["anyOf"][0]["type"] == "string"


async def test_empty_secrets_never_pass(client, monkeypatch):
    from src.core.config import get_settings

    r = await client.get("/internal/platform/gpu-nodes", headers={"X-Platform-Token": ""})
    assert r.status_code == 401
    r = await client.post("/internal/ingest/auth", json={"action": "read", "user": "", "password": ""})
    assert r.status_code == 403
    # and when the server itself was started without them
    monkeypatch.setattr(get_settings(), "platform_token", "")
    monkeypatch.setattr(get_settings(), "ingest_hook_secret", "")
    assert (await client.get("/internal/platform/gpu-nodes", headers={"X-Platform-Token": ""})).status_code == 401
    assert (await client.post("/internal/ingest/auth?secret=", json={"action": "read"})).status_code == 403


async def test_audit_cursor_round_trip(client):
    t = await register(client, "o@a.kz")
    for i in range(3):
        await push_camera(client, t, f"c{i}")
    page = (await client.get("/api/v1/audit", headers=bearer(t), params={"limit": 2})).json()
    assert isinstance(page["next_cursor"], str)
    rest = (await client.get("/api/v1/audit", headers=bearer(t), params={"cursor": page["next_cursor"]})).json()
    assert len(page["items"]) + len(rest["items"]) == 4  # registered + 3 cameras
    assert (await client.get("/api/v1/audit", headers=bearer(t), params={"cursor": "abc"})).status_code == 400
