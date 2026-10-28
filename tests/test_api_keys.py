from httpx import AsyncClient

from tests.conftest import Factory


async def test_api_key_lifecycle(client: AsyncClient, factory: Factory) -> None:
    admin = await factory.user(await factory.tenant(), ["tenant_admin"])

    created = await client.post(
        "/api/v1/api-keys",
        json={"name": "ERP sync", "scopes": ["user:read", "vendor:read"]},
        headers=admin.headers,
    )
    assert created.status_code == 201, created.text
    secret = created.json()["key"]
    assert secret.startswith("mai_live_")

    listed = await client.get("/api/v1/api-keys", headers=admin.headers)
    assert "key" not in listed.json()[0] and "key_hash" not in listed.json()[0]

    as_key = {"X-API-Key": secret}
    assert (await client.get("/api/v1/users", headers=as_key)).status_code == 200
    # Scope not granted to the key:
    assert (await client.get("/api/v1/audit-logs", headers=as_key)).status_code == 403
    # Keys can't manage keys:
    assert (await client.get("/api/v1/api-keys", headers=as_key)).status_code == 403

    tampered = secret[:-1] + ("A" if secret[-1] != "A" else "B")
    assert (await client.get("/api/v1/users", headers={"X-API-Key": tampered})).status_code == 401

    assert (
        await client.delete(f"/api/v1/api-keys/{created.json()['id']}", headers=admin.headers)
    ).status_code == 204
    assert (await client.get("/api/v1/users", headers=as_key)).status_code == 401


async def test_api_key_scopes_cannot_exceed_creator_or_allowed_set(
    client: AsyncClient, factory: Factory
) -> None:
    admin = await factory.user(await factory.tenant(), ["tenant_admin"])
    resp = await client.post(
        "/api/v1/api-keys",
        json={"name": "bad", "scopes": ["apikey:manage", "knowledge:approve"]},
        headers=admin.headers,
    )
    assert resp.status_code == 422
    assert len(resp.json()["errors"]) >= 2


async def test_api_key_ip_allowlist(client: AsyncClient, factory: Factory) -> None:
    admin = await factory.user(await factory.tenant(), ["tenant_admin"])
    created = await client.post(
        "/api/v1/api-keys",
        json={"name": "office only", "scopes": ["vendor:read"], "ip_allowlist": ["203.0.113.0/24"]},
        headers=admin.headers,
    )
    resp = await client.get("/api/v1/jobs", headers={"X-API-Key": created.json()["key"]})
    assert resp.status_code == 403
    assert resp.json()["code"] == "ip_not_allowed"
