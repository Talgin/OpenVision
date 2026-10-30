from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select

from src.db.session import SessionLocal
from src.models import Camera
from src.schemas import ProbeOut, StreamInfo
from src.services import probe
from tests.conftest import add_user, bearer, push_camera, register

HOOK = "/internal/ingest/auth?secret=hook-secret"


def _srt_creds(srt_url: str) -> tuple[str, str, str]:
    streamid = parse_qs(urlsplit(srt_url).query)["streamid"][0]
    _, path, user, password = streamid.split(":", 3)
    return path, user, password


async def _hook(client, **body):
    return await client.post(HOOK, json={"ip": "1.2.3.4", "protocol": "srt", **body})


async def test_push_camera_key_works_once_shown(client):
    t = await register(client, "o@a.kz")
    cam = await push_camera(client, t)
    assert cam["status"] == "pending" and cam["mode"] == "push"
    path, user, key = _srt_creds(cam["push"]["srt_url"])
    assert path == f"cam/{cam['id']}" and key == cam["push"]["stream_key"]
    assert "ingest.test:8890" in cam["push"]["srt_url"]

    assert (await _hook(client, action="publish", path=path, user=user, password=key)).status_code == 200
    assert (await _hook(client, action="publish", path=path, user=user, password=key + "x")).status_code == 401
    # the key is never shown again
    assert "push" not in (await client.get(f"/api/v1/cameras/{cam['id']}", headers=bearer(t))).json()


async def test_rotate_key_invalidates_old_key(client):
    t = await register(client, "o@a.kz")
    cam = await push_camera(client, t)
    old = cam["push"]["stream_key"]
    new = (await client.post(f"/api/v1/cameras/{cam['id']}/rotate-key", headers=bearer(t))).json()["stream_key"]
    path = f"cam/{cam['id']}"
    assert (await _hook(client, action="publish", path=path, user="push", password=old)).status_code == 401
    assert (await _hook(client, action="publish", path=path, user="push", password=new)).status_code == 200


async def test_key_of_one_camera_does_not_open_another(client):
    t = await register(client, "o@a.kz")
    c1, c2 = await push_camera(client, t, "one"), await push_camera(client, t, "two")
    r = await _hook(client, action="publish", path=f"cam/{c2['id']}", user="push", password=c1["push"]["stream_key"])
    assert r.status_code == 401


async def test_hook_requires_secret_and_internal_reader(client):
    t = await register(client, "o@a.kz")
    cam = await push_camera(client, t)
    body = {"action": "read", "path": f"cam/{cam['id']}", "user": "ovinternal", "password": "internal-pass"}
    assert (await client.post("/internal/ingest/auth", json=body)).status_code == 403
    assert (await _hook(client, **body)).status_code == 200
    # a client's own push key does not let it read (or anyone else)
    assert (await _hook(client, **{**body, "user": "push", "password": cam["push"]["stream_key"]})).status_code == 401
    assert (await _hook(client, action="api", user="", password="")).status_code == 401
    assert (await _hook(client, action="publish", path="other/thing", user="push", password="x")).status_code == 401


async def test_disabled_or_deleted_camera_cannot_publish(client):
    t = await register(client, "o@a.kz")
    cam = await push_camera(client, t)
    creds = {"action": "publish", "path": f"cam/{cam['id']}", "user": "push", "password": cam["push"]["stream_key"]}
    await client.patch(f"/api/v1/cameras/{cam['id']}", headers=bearer(t), json={"enabled": False})
    assert (await _hook(client, **creds)).status_code == 401
    await client.patch(f"/api/v1/cameras/{cam['id']}", headers=bearer(t), json={"enabled": True})
    assert (await _hook(client, **creds)).status_code == 200
    assert (await client.delete(f"/api/v1/cameras/{cam['id']}", headers=bearer(t))).status_code == 204
    assert (await _hook(client, **creds)).status_code == 401
    assert (await client.get(f"/api/v1/cameras/{cam['id']}", headers=bearer(t))).status_code == 404


async def test_edge_agent_token_publishes_only_its_cameras(client):
    t = await register(client, "o@a.kz")
    a1 = (await client.post("/api/v1/edge-agents", headers=bearer(t), json={"name": "pc1"})).json()
    a2 = (await client.post("/api/v1/edge-agents", headers=bearer(t), json={"name": "pc2"})).json()
    assert a1["token"] in a1["install_command"]
    cam = (
        await client.post(
            "/api/v1/cameras",
            headers=bearer(t),
            json={"name": "lan", "mode": "edge", "source_url": "rtsp://u:p@192.168.1.10/s", "edge_agent_id": a1["id"]},
        )
    ).json()
    assert cam["source_url"] == "rtsp://***@192.168.1.10/s"
    path = f"cam/{cam['id']}"
    assert (await _hook(client, action="publish", path=path, user="edge", password=a1["token"])).status_code == 200
    assert (await _hook(client, action="publish", path=path, user="edge", password=a2["token"])).status_code == 401

    cfg = await client.get("/api/v1/edge/config", headers={"Authorization": f"Bearer {a1['token']}"})
    assert cfg.status_code == 200
    streams = cfg.json()["streams"]
    assert len(streams) == 1 and streams[0]["source_url"] == "rtsp://u:p@192.168.1.10/s"
    assert _srt_creds(streams[0]["publish_url"]) == (path, "edge", a1["token"])
    other = await client.get("/api/v1/edge/config", headers={"Authorization": f"Bearer {a2['token']}"})
    assert other.json()["streams"] == []
    assert (await client.get("/api/v1/edge/config", headers={"Authorization": "Bearer bad"})).status_code == 401
    agents = (await client.get("/api/v1/edge-agents", headers=bearer(t))).json()
    assert {a["name"]: a["online"] for a in agents} == {"pc1": True, "pc2": True}
    assert all("token" not in a for a in agents)


async def test_pull_to_private_address_is_refused(client):
    t = await register(client, "o@a.kz")
    for url in ("rtsp://127.0.0.1:554/x", "rtsp://10.0.0.5/x", "rtsp://169.254.169.254/latest", "rtsp://[::1]/x"):
        r = await client.post("/api/v1/cameras", headers=bearer(t), json={"name": "x", "mode": "pull", "source_url": url})
        assert r.status_code == 422, url
        assert r.json()["error"]["details"]["reason"] == "private_address_refused", url
    r = await client.post("/api/v1/cameras/probe", headers=bearer(t), json={"url": "http://example.com/x"})
    assert r.json() == {"ok": False, "reason": "invalid_url", "stream": None, "warnings": []}


async def test_pull_camera_stores_url_encrypted_and_shows_it_redacted(client, monkeypatch):
    async def fake_probe(url):
        return ProbeOut(ok=True, stream=StreamInfo(codec="h264", width=1920, height=1080, fps=25))

    monkeypatch.setattr(probe, "probe", fake_probe)
    t = await register(client, "o@a.kz")
    url = "rtsp://admin:S3cret!@cam.example.com:554/Streaming/101?token=abc"
    r = await client.post("/api/v1/cameras", headers=bearer(t), json={"name": "door", "mode": "pull", "source_url": url})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_url"] == "rtsp://***@cam.example.com:554/Streaming/101"
    assert body["stream_info"]["width"] == 1920
    assert "S3cret" not in r.text
    async with SessionLocal() as s:
        row = (await s.execute(select(Camera))).scalar_one()
    assert "S3cret" not in row.source_url_enc and "admin" not in row.source_url_enc


async def test_probe_failure_is_reported_with_reason(client, monkeypatch):
    async def fake_probe(url):
        return ProbeOut(ok=False, reason="source_auth_failed")

    monkeypatch.setattr(probe, "probe", fake_probe)
    t = await register(client, "o@a.kz")
    r = await client.post(
        "/api/v1/cameras", headers=bearer(t), json={"name": "d", "mode": "pull", "source_url": "rtsp://cam.example.com/x"}
    )
    assert r.status_code == 422
    assert r.json()["error"] == {
        "code": "probe_failed",
        "message": "the stream could not be opened",
        "details": {"reason": "source_auth_failed", "stream": None},
    }


async def test_camera_quota(client):
    t = await register(client, "o@a.kz")
    for i in range(4):
        await push_camera(client, t, f"c{i}")
    r = await client.post("/api/v1/cameras", headers=bearer(t), json={"name": "c5", "mode": "push"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "camera_limit_reached"
    assert r.json()["error"]["details"] == {"limit": 4}


async def test_roles(client):
    owner = await register(client, "o@a.kz")
    cam = await push_camera(client, owner)
    viewer = await add_user(client, owner, "v@a.kz", "viewer")
    operator = await add_user(client, owner, "op@a.kz", "operator")
    admin = await add_user(client, owner, "ad@a.kz", "admin")
    path = f"/api/v1/cameras/{cam['id']}"

    assert (await client.get(path, headers=bearer(viewer))).status_code == 200
    r = await client.post("/api/v1/cameras", headers=bearer(viewer), json={"name": "x", "mode": "push"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert (await client.patch(path, headers=bearer(viewer), json={"enabled": False})).status_code == 403
    assert (await client.get("/api/v1/users", headers=bearer(viewer))).status_code == 403

    assert (await client.patch(path, headers=bearer(operator), json={"enabled": False})).status_code == 200
    assert (await client.patch(path, headers=bearer(operator), json={"name": "x"})).status_code == 403
    assert (await client.delete(path, headers=bearer(operator))).status_code == 403

    assert (await client.patch(path, headers=bearer(admin), json={"name": "renamed"})).status_code == 200
    me = (await client.get("/api/v1/auth/me", headers=bearer(owner))).json()["user"]["id"]
    r = await client.patch(f"/api/v1/users/{me}", headers=bearer(admin), json={"role": "viewer"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "owner_protected"
    r = await client.post(
        "/api/v1/users", headers=bearer(admin),
        json={"email": "o2@a.kz", "full_name": "x", "role": "owner", "password": "long-enough-pass"},
    )  # fmt: skip
    assert r.status_code == 422


async def test_demotion_applies_to_existing_token(client):
    owner = await register(client, "o@a.kz")
    admin = await add_user(client, owner, "ad@a.kz", "admin")
    admin_id = (await client.get("/api/v1/auth/me", headers=bearer(admin))).json()["user"]["id"]
    assert (await client.get("/api/v1/users", headers=bearer(admin))).status_code == 200
    await client.patch(f"/api/v1/users/{admin_id}", headers=bearer(owner), json={"role": "viewer"})
    assert (await client.get("/api/v1/users", headers=bearer(admin))).status_code == 403
    await client.patch(f"/api/v1/users/{admin_id}", headers=bearer(owner), json={"is_active": False})
    assert (await client.get("/api/v1/auth/me", headers=bearer(admin))).status_code == 401
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": admin["refresh_token"]})
    assert r.status_code == 401
