import re

import pytest
from httpx import AsyncClient

from app.core.container import Container
from tests.conftest import PASSWORD, Factory, RecordingEmailSender

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"


async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"]


async def test_login_returns_token_cookie_and_profile(client: AsyncClient, factory: Factory) -> None:
    tenant = await factory.tenant()
    actor = await factory.user(tenant, ["engineer"], login=False)

    resp = await client.post(LOGIN, json={"email": actor.email, "password": PASSWORD})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["user"]["tenant"]["id"] == str(tenant)
    assert body["user"]["roles"] == ["engineer"]
    assert "discovery:run" in body["user"]["permissions"]
    assert "user:manage" not in body["user"]["permissions"]
    cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Path=/api/v1/auth" in cookie and "samesite=strict" in cookie.lower()

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == actor.email


async def test_wrong_password_is_generic_and_locks_after_threshold(
    client: AsyncClient, factory: Factory
) -> None:
    actor = await factory.user(await factory.tenant(), ["viewer"], login=False)

    for _ in range(5):
        resp = await client.post(LOGIN, json={"email": actor.email, "password": "wrong-password"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_credentials"
        assert resp.headers["content-type"].startswith("application/problem+json")

    locked = await client.post(LOGIN, json={"email": actor.email, "password": PASSWORD})
    assert locked.status_code == 423


async def test_unknown_email_is_indistinguishable(client: AsyncClient) -> None:
    resp = await client.post(LOGIN, json={"email": "nobody@example.com", "password": "whatever"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


async def test_refresh_rotation_and_reuse_detection(
    client: AsyncClient, factory: Factory, container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = await factory.user(await factory.tenant(), ["viewer"], login=False)
    await client.post(LOGIN, json={"email": actor.email, "password": PASSWORD})
    first_rt = client.cookies.get("mai_rt")
    assert first_rt

    rotated = await client.post(REFRESH)
    assert rotated.status_code == 200
    second_rt = client.cookies.get("mai_rt")
    assert second_rt and second_rt != first_rt

    # Replayed immediately (two tabs refreshing at once): rejected, but the session survives.
    client.cookies.set("mai_rt", first_rt, path="/api/v1/auth")
    concurrent = await client.post(REFRESH)
    assert concurrent.status_code == 401
    assert concurrent.json()["code"] == "refresh_conflict"

    # Replayed after the grace window: treated as theft, the whole family is revoked.
    monkeypatch.setattr(container.settings, "refresh_reuse_grace_seconds", 0)
    client.cookies.set("mai_rt", first_rt, path="/api/v1/auth")
    replay = await client.post(REFRESH)
    assert replay.status_code == 401
    assert replay.json()["code"] == "unauthenticated"
    client.cookies.set("mai_rt", second_rt, path="/api/v1/auth")
    after = await client.post(REFRESH)
    assert after.status_code == 401


async def test_logout_revokes_refresh_token(client: AsyncClient, factory: Factory) -> None:
    actor = await factory.user(await factory.tenant(), ["viewer"], login=False)
    await client.post(LOGIN, json={"email": actor.email, "password": PASSWORD})
    rt = client.cookies.get("mai_rt")

    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    client.cookies.set("mai_rt", rt, path="/api/v1/auth")
    assert (await client.post(REFRESH)).status_code == 401


async def test_suspended_tenant_cannot_sign_in(client: AsyncClient, factory: Factory) -> None:
    from app.domain.enums import TenantStatus

    actor = await factory.user(await factory.tenant(TenantStatus.SUSPENDED), ["viewer"], login=False)
    resp = await client.post(LOGIN, json={"email": actor.email, "password": PASSWORD})
    assert resp.status_code == 403
    assert resp.json()["code"] == "tenant_suspended"


async def test_invite_accept_flow(
    client: AsyncClient, factory: Factory, emails: RecordingEmailSender
) -> None:
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])

    invited = await client.post(
        "/api/v1/users/invite",
        json={"email": "new.engineer@example.com", "full_name": "New Engineer", "roles": ["engineer"]},
        headers=admin.headers,
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["status"] == "invited"

    token = re.search(r"token=([\w-]+)", emails.last_to("new.engineer@example.com").body).group(1)  # type: ignore[union-attr]
    weak = await client.post("/api/v1/auth/invitations/accept", json={"token": token, "password": "short"})
    assert weak.status_code == 422

    accepted = await client.post(
        "/api/v1/auth/invitations/accept", json={"token": token, "password": PASSWORD}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["user"]["roles"] == ["engineer"]

    again = await client.post("/api/v1/auth/invitations/accept", json={"token": token, "password": PASSWORD})
    assert again.status_code == 400


async def test_password_reset_flow(
    client: AsyncClient, factory: Factory, emails: RecordingEmailSender
) -> None:
    actor = await factory.user(await factory.tenant(), ["viewer"], login=False)

    resp = await client.post("/api/v1/auth/password/forgot", json={"email": actor.email})
    assert resp.status_code == 202
    token = re.search(r"token=([\w-]+)", emails.last_to(actor.email).body).group(1)  # type: ignore[union-attr]

    new_password = "An0ther-Str0ng-One!"
    assert (
        await client.post("/api/v1/auth/password/reset", json={"token": token, "new_password": new_password})
    ).status_code == 204
    assert (await client.post(LOGIN, json={"email": actor.email, "password": PASSWORD})).status_code == 401
    assert (
        await client.post(LOGIN, json={"email": actor.email, "password": new_password})
    ).status_code == 200


async def test_missing_and_bad_tokens(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    bad = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert bad.status_code == 401
    assert bad.json()["code"] == "unauthenticated"
