import os
import tempfile

from cryptography.fernet import Fernet

_tmp = tempfile.mkdtemp(prefix="ov-test-")
os.environ.update(
    {
        "OV_DATABASE_URL": f"sqlite+aiosqlite:///{_tmp}/test.db",
        "OV_JWT_SECRET": "test-jwt-secret-" + "x" * 32,
        "OV_SECRETS_KEY": Fernet.generate_key().decode(),
        "OV_INGEST_HOOK_SECRET": "hook-secret",
        "OV_INGEST_INTERNAL_PASSWORD": "internal-pass",
        "OV_PLATFORM_TOKEN": "platform-token",
        "OV_MEDIA_LOCAL_DIR": f"{_tmp}/media",
        "OV_PUBLIC_BASE_URL": "http://test",
        "OV_INGEST_PUBLIC_HOST": "ingest.test",
    }
)

import fakeredis  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from src.db.session import engine  # noqa: E402
from src.main import app  # noqa: E402
from src.models import Base  # noqa: E402
from src.services import redis as redis_service  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture
def fake_server():
    return fakeredis.FakeServer()


@pytest_asyncio.fixture(autouse=True)
async def fresh_state(fake_server):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    redis_service.set_client(fakeredis.FakeAsyncRedis(server=fake_server))
    yield
    redis_service.set_client(None)


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def register(client, email: str, company: str = "Acme") -> dict:
    r = await client.post(
        "/api/v1/auth/register",
        json={"company_name": company, "full_name": "Owner", "email": email, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    return r.json()


def bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def add_user(client, owner_tokens: dict, email: str, role: str) -> dict:
    r = await client.post(
        "/api/v1/users",
        headers=bearer(owner_tokens),
        json={"email": email, "full_name": role.title(), "role": role, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


async def push_camera(client, tokens: dict, name: str = "Door") -> dict:
    r = await client.post("/api/v1/cameras", headers=bearer(tokens), json={"name": name, "mode": "push"})
    assert r.status_code == 201, r.text
    return r.json()
