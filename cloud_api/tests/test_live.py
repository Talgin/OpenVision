"""WebSocket protocol. Runs in Starlette's sync TestClient (own event loop)."""

import fakeredis
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.main import app
from src.services import redis as redis_service
from tests.conftest import PASSWORD


@pytest.fixture
def sync_client(fake_server):
    redis_service.set_client(fakeredis.FakeAsyncRedis(server=fake_server))
    with TestClient(app) as c:
        yield c


def _signup(c, email, company):
    r = c.post(
        "/api/v1/auth/register",
        json={"company_name": company, "full_name": "O", "email": email, "password": PASSWORD},
    )
    return r.json()


def _camera(c, tokens, name):
    r = c.post("/api/v1/cameras", headers={"Authorization": f"Bearer {tokens['access_token']}"},
               json={"name": name, "mode": "push"})  # fmt: skip
    return r.json()


def test_auth_required_first(sync_client):
    with sync_client.websocket_connect("/api/v1/live") as ws:
        ws.send_json({"type": "subscribe"})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    assert e.value.code == 4401

    with sync_client.websocket_connect("/api/v1/live") as ws:
        ws.send_json({"type": "auth", "token": "bad"})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    assert e.value.code == 4401


def test_tenant_only_sees_its_own_live_messages(sync_client):
    a = _signup(sync_client, "a@a.kz", "Alpha")
    b = _signup(sync_client, "b@b.kz", "Beta")
    with sync_client.websocket_connect("/api/v1/live") as ws_b:
        ws_b.send_json({"type": "auth", "token": b["access_token"]})
        assert ws_b.receive_json()["type"] == "ready"
        _camera(sync_client, a, "alpha-cam")  # publishes camera_status to Alpha only
        _camera(sync_client, b, "beta-cam")
        msg = ws_b.receive_json()
        assert msg["type"] == "camera_status" and msg["data"]["name"] == "beta-cam"
