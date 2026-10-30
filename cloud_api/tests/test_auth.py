from tests.conftest import PASSWORD, bearer, register


async def test_register_then_me(client):
    tokens = await register(client, "owner@a.kz", "Acme")
    assert tokens["token_type"] == "bearer" and tokens["expires_in"] == 900
    r = await client.get("/api/v1/auth/me", headers=bearer(tokens))
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["role"] == "owner"
    assert body["tenant"]["name"] == "Acme"
    assert "account.write" in body["permissions"]


async def test_duplicate_email_is_409(client):
    await register(client, "dup@a.kz")
    r = await client.post(
        "/api/v1/auth/register",
        json={"company_name": "Xyz", "full_name": "Y", "email": "DUP@a.kz", "password": PASSWORD},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"


async def test_wrong_password_and_unknown_email_look_the_same(client):
    await register(client, "u@a.kz")
    wrong = await client.post("/api/v1/auth/login", json={"email": "u@a.kz", "password": "nope-nope-nope"})
    missing = await client.post("/api/v1/auth/login", json={"email": "ghost@a.kz", "password": "nope-nope-nope"})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()
    assert wrong.json()["error"]["code"] == "invalid_credentials"


async def test_login_is_rate_limited(client):
    await register(client, "rl@a.kz")
    codes = [
        (await client.post("/api/v1/auth/login", json={"email": "rl@a.kz", "password": "bad-password!"})).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429


async def test_validation_error_shape(client):
    r = await client.post("/api/v1/auth/register", json={"company_name": "A", "email": "not-an-email"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert {d["field"] for d in err["details"]} >= {"email", "password", "full_name"}


async def test_refresh_rotates_and_reuse_kills_the_session(client):
    first = await register(client, "rot@a.kz")
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 200
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]

    # the old one again: treated as stolen, whole family revoked
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_refresh_token"
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert r.status_code == 401


async def test_logout_revokes_refresh(client):
    tokens = await register(client, "out@a.kz")
    assert (await client.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})).status_code == 204
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401


async def test_password_change_revokes_sessions(client):
    tokens = await register(client, "pw@a.kz")
    r = await client.put(
        "/api/v1/auth/me/password",
        headers=bearer(tokens),
        json={"current_password": PASSWORD, "new_password": "another-long-password"},
    )
    assert r.status_code == 204
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401
    r = await client.post("/api/v1/auth/login", json={"email": "pw@a.kz", "password": "another-long-password"})
    assert r.status_code == 200


async def test_garbage_token_is_401(client):
    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nonsense"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert (await client.get("/api/v1/auth/me")).status_code == 401
