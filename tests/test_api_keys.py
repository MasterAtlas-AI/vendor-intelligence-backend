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


async def test_api_key_edit_and_delete(client: AsyncClient, factory: Factory) -> None:
    admin = await factory.user(await factory.tenant(), ["tenant_admin"])
    outsider = await factory.user(await factory.tenant(), ["tenant_admin"])
    created = (await client.post("/api/v1/api-keys", headers=admin.headers,
                                 json={"name": "ERP sync", "scopes": ["vendor:read"]})).json()  # fmt: skip
    url, as_key = f"/api/v1/api-keys/{created['id']}", {"X-API-Key": created["key"]}
    assert (await client.get("/api/v1/users", headers=as_key)).status_code == 403

    # Edit: name, scopes, limit and expiry change; the same secret now has the new scope at once.
    edited = await client.patch(url, headers=admin.headers, json={
        "name": "ERP sync (prod)", "scopes": ["vendor:read", "user:read"], "rate_limit_per_minute": 300,
        "expires_at": "2099-01-01T00:00:00Z",
    })  # fmt: skip
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["name"], body["scopes"], body["rate_limit_per_minute"]) == (
        "ERP sync (prod)", ["user:read", "vendor:read"], 300)  # fmt: skip
    assert "key" not in body and body["expires_at"].startswith("2099-01-01")
    assert (await client.get("/api/v1/users", headers=as_key)).status_code == 200
    never = await client.patch(url, headers=admin.headers, json={"expires_at": None})
    assert never.json()["expires_at"] is None and never.json()["name"] == "ERP sync (prod)"

    # The same rules as creating: no scope the editor lacks or keys can't hold; another organization can't edit.
    bad = await client.patch(url, headers=admin.headers, json={"scopes": ["apikey:manage"]})
    assert bad.status_code == 422
    assert (await client.patch(url, headers=outsider.headers, json={"name": "x"})).status_code == 404

    # Revoked keys can't be edited; delete removes a key from the list for good.
    assert (await client.delete(url, headers=admin.headers)).status_code == 204
    assert (await client.patch(url, headers=admin.headers, json={"name": "y"})).status_code == 422
    assert (await client.delete(f"{url}?permanent=true", headers=outsider.headers)).status_code == 404
    assert (await client.delete(f"{url}?permanent=true", headers=admin.headers)).status_code == 204
    assert all(
        k["id"] != created["id"] for k in (await client.get("/api/v1/api-keys", headers=admin.headers)).json()
    )
    audit = (
        await client.get("/api/v1/audit-logs", headers=admin.headers, params={"entity_id": created["id"]})
    ).json()
    actions = {e["action"] for e in (audit["items"] if isinstance(audit, dict) else audit)}
    assert {"api_key.updated", "api_key.deleted"} <= actions
