"""Known persons, names on events, and erasing faces and images."""

import json
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update

from src.core.config import get_settings
from src.core.security import utcnow
from src.db.session import SessionLocal
from src.models import Event, Plan, Tenant
from src.services import faces
from src.workers import bridge
from src.workers.demo import fake_message
from src.workers.scheduler import Scheduler
from tests.conftest import PASSWORD, add_user, bearer, push_camera, register
from tests.test_scheduler import FakeMTX, FakeStreamer

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


async def face_plan(client, tokens) -> str:
    tid = (await client.get("/api/v1/auth/me", headers=bearer(tokens))).json()["tenant"]["id"]
    async with SessionLocal() as s:
        await s.execute(update(Tenant).values(plan=Plan.FACE))
        await s.commit()
    return tid


async def enroll(client, tokens, name="Aigerim", photo=JPEG, ctype="image/jpeg", **form):
    return await client.post(
        "/api/v1/persons", headers=bearer(tokens), data={"name": name, **form}, files={"photo": ("a.jpg", photo, ctype)}
    )


async def known_event(camera_id: int, person_id: str) -> None:
    msg = fake_message(camera_id)
    msg.update(face_id=person_id, face_image_url="http://x/database-photos-bucket/p/1.jpg")
    async with SessionLocal() as s:
        await bridge.handle(s, msg)


@pytest.fixture
def demo_faces(monkeypatch):
    monkeypatch.setattr(get_settings(), "face_api_url", "demo")


async def test_enrollment_needs_the_face_plan(client, demo_faces):
    t = await register(client, "o@a.kz")
    r = await enroll(client, t)
    assert r.status_code == 403 and r.json()["error"]["code"] == "face_plan_required"


async def test_enrollment_off_when_no_face_service(client):
    t = await register(client, "o@a.kz")
    await face_plan(client, t)
    r = await enroll(client, t)
    assert r.status_code == 503 and r.json()["error"]["code"] == "face_service_unavailable"


async def test_person_lifecycle_and_names_on_events(client, demo_faces):
    t = await register(client, "o@a.kz")
    await face_plan(client, t)
    cam = await push_camera(client, t)

    r = await enroll(client, t, note="night shift")
    assert r.status_code == 201, r.text
    person = r.json()
    assert person["note"] == "night shift" and person["media_expires_at"]
    photo = await client.get(person["photo_url"].removeprefix("http://test"))
    assert photo.status_code == 200 and photo.content == JPEG

    await known_event(cam["id"], person["id"])
    events = (await client.get("/api/v1/events", headers=bearer(t))).json()["items"]
    assert events[0]["person_name"] == "Aigerim"
    one = (await client.get(f"/api/v1/events/{events[0]['id']}", headers=bearer(t))).json()
    assert one["person_name"] == "Aigerim"

    path = f"/api/v1/persons/{person['id']}"
    body = (await client.patch(path, headers=bearer(t), json={"name": " Aigerim S. ", "note": None})).json()
    assert body["name"] == "Aigerim S." and body["note"] is None
    assert (await client.patch(path, headers=bearer(t), json={"name": None})).status_code == 422

    r = await client.put(f"{path}/photo", headers=bearer(t), files={"photo": ("b.png", b"\x89PNG-new", "image/png")})
    assert r.status_code == 200
    assert (await client.get(r.json()["photo_url"].removeprefix("http://test"))).content == b"\x89PNG-new"

    assert (await client.delete(path, headers=bearer(t))).status_code == 204
    assert (await client.get(path, headers=bearer(t))).status_code == 404
    assert not list((Path(get_settings().media_local_dir) / "database-photos-bucket" / person["id"]).glob("*"))
    events = (await client.get("/api/v1/events", headers=bearer(t))).json()["items"]
    assert events[0]["person_id"] == person["id"] and events[0]["person_name"] is None


async def test_roles_search_and_paging(client, demo_faces):
    t = await register(client, "o@a.kz")
    await face_plan(client, t)
    for name in ("Bolat", "aliya", "Askar"):
        assert (await enroll(client, t, name=name)).status_code == 201
    viewer = await add_user(client, t, "v@a.kz", "viewer")
    op = await add_user(client, t, "op@a.kz", "operator")

    page = (await client.get("/api/v1/persons", headers=bearer(viewer), params={"limit": 2})).json()
    assert [x["name"] for x in page["items"]] == ["aliya", "Askar"]
    rest = (await client.get("/api/v1/persons", headers=bearer(viewer), params={"cursor": page["next_cursor"]})).json()
    assert len(page["items"]) + len(rest["items"]) == 3 and rest["next_cursor"] is None
    found = (await client.get("/api/v1/persons", headers=bearer(t), params={"q": "AL"})).json()["items"]
    assert [x["name"] for x in found] == ["aliya"]

    assert (await enroll(client, viewer)).status_code == 403
    assert (await enroll(client, op)).status_code == 403
    me = (await client.get("/api/v1/auth/me", headers=bearer(op))).json()
    assert "persons.read" in me["permissions"] and "persons.write" not in me["permissions"]


async def test_persons_and_names_stay_in_their_tenant(client, demo_faces):
    a = await register(client, "o@a.kz", "A LLP")
    b = await register(client, "o@b.kz", "B LLP")
    await face_plan(client, a)
    person = (await enroll(client, a)).json()
    cam_b = await push_camera(client, b)

    assert (await client.get(f"/api/v1/persons/{person['id']}", headers=bearer(b))).status_code == 404
    assert (await client.delete(f"/api/v1/persons/{person['id']}", headers=bearer(b))).status_code == 404
    assert (await client.get("/api/v1/persons", headers=bearer(b))).json()["items"] == []
    await known_event(cam_b["id"], person["id"])  # B's event carrying A's person id
    assert (await client.get("/api/v1/events", headers=bearer(b))).json()["items"][0]["person_name"] is None


async def test_photo_checks(client, demo_faces, monkeypatch):
    t = await register(client, "o@a.kz")
    await face_plan(client, t)
    r = await enroll(client, t, photo=b"GIF89a", ctype="image/gif")
    assert r.json()["error"]["code"] == "invalid_photo"
    monkeypatch.setattr(get_settings(), "max_photo_bytes", 10)
    r = await enroll(client, t, photo=b"x" * 11)
    assert r.status_code == 422 and r.json()["error"]["code"] == "photo_too_large"
    monkeypatch.setattr(get_settings(), "max_persons", 0)
    assert (await enroll(client, t)).json()["error"]["code"] == "person_limit_reached"


# trt_api over HTTP --------------------------------------------------------------

@pytest.fixture
def trt_api(monkeypatch):
    """Routes faces' httpx calls to a handler; returns the list of requests seen."""
    seen, state = [], {"insert": 200, "delete_tenant": 200}
    monkeypatch.setattr(get_settings(), "face_api_url", "http://trt-api:8000")
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/insert_face/":
            if state["insert"] == 400:
                return httpx.Response(400, json={"status": "error", "reason": "No faces detected in the image"})
            return httpx.Response(state["insert"], json={"status": "accepted", "object_name": "pid/emb.jpg"})
        if request.url.path == "/delete_tenant/":
            return httpx.Response(state["delete_tenant"], json={"status": "deleted", "deleted": {}})
        return httpx.Response(200, json={})

    monkeypatch.setattr(faces.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen, state


async def test_enroll_sends_tenant_and_maps_errors(client, trt_api):
    seen, state = trt_api
    t = await register(client, "o@a.kz")
    tid = await face_plan(client, t)
    r = await enroll(client, t)
    assert r.status_code == 201
    body = seen[-1].content.decode(errors="replace")
    assert f'name="tenant_id"\r\n\r\n{tid}' in body and f'name="person_id"\r\n\r\n{r.json()["id"]}' in body
    assert 'name="category_ids"\r\n\r\ncloud' in body

    state["insert"] = 400
    assert (await enroll(client, t)).json()["error"]["code"] == "no_face_found"
    state["insert"] = 500
    assert (await enroll(client, t)).status_code == 503
    assert len((await client.get("/api/v1/persons", headers=bearer(t))).json()["items"]) == 1

    await client.delete(f"/api/v1/persons/{r.json()['id']}", headers=bearer(t))
    assert seen[-1].url.path == "/delete_face/" and json.loads(seen[-1].content) == {"person_id": r.json()["id"]}


async def test_deleted_company_faces_are_erased_before_the_company_goes(client, trt_api):
    seen, state = trt_api
    t = await register(client, "o@a.kz")
    tid = await face_plan(client, t)
    await enroll(client, t)
    await client.post("/api/v1/auth/delete-account", headers=bearer(t), json={"password": PASSWORD})

    sched = Scheduler(FakeMTX(), lambda n: FakeStreamer())
    state["delete_tenant"] = 503  # trt_api down: the company must wait
    for _ in range(2):
        async with SessionLocal() as s:
            await sched.tick(s)
    async with SessionLocal() as s:
        assert (await s.get(Tenant, uuid.UUID(tid))) is not None

    state["delete_tenant"] = 200
    async with SessionLocal() as s:
        await sched.tick(s)
    async with SessionLocal() as s:
        assert (await s.execute(select(Tenant))).scalars().all() == []
    assert json.loads(seen[-1].content) == {"tenant_id": tid}


# images go with their events --------------------------------------------------------

async def test_expired_events_take_their_images(client, monkeypatch):
    t = await register(client, "o@a.kz")
    cam = await push_camera(client, t)
    root = Path(get_settings().media_local_dir)
    for key in ("face-crops-bucket/old.jpg", "face-full-frames-bucket/old.jpg", "face-crops-bucket/new.jpg"):
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        (root / key).write_bytes(b"x")
    for when, name in ((utcnow() - timedelta(days=40), "old"), (utcnow(), "new")):
        msg = fake_message(cam["id"])
        msg.update(
            crop_image_url=f"http://m/face-crops-bucket/{name}.jpg",
            frame_image_url=f"http://m/face-full-frames-bucket/{name}.jpg" if name == "old" else None,
            datetime=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )  # fmt: skip
        async with SessionLocal() as s:
            await bridge.handle(s, msg)

    async with SessionLocal() as s:
        await Scheduler(FakeMTX(), lambda n: FakeStreamer())._retention(s)
        left = (await s.execute(select(Event.crop_key))).scalars().all()
    assert left == ["face-crops-bucket/new.jpg"]
    assert sorted(p.name for p in root.rglob("*.jpg") if p.parent.name.endswith("bucket")) == ["new.jpg"]

    # the demo shares placeholder images between events: it turns deletion off
    monkeypatch.setattr(get_settings(), "media_delete_with_events", False)
    async with SessionLocal() as s:
        await s.execute(update(Event).values(occurred_at=utcnow() - timedelta(days=40)))
        await s.commit()
        await Scheduler(FakeMTX(), lambda n: FakeStreamer())._retention(s)
    assert (root / "face-crops-bucket/new.jpg").exists()
