import re
import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import Factory, RecordingEmailSender

# (method, path, body) for endpoints guarded by a single permission, and the tenant roles allowed to call them.
GUARDED = [
    ("GET", "/api/v1/users", None, {"tenant_admin", "manager"}),
    (
        "POST",
        "/api/v1/users/invite",
        {"email": "rbac-{n}@example.com", "full_name": "X", "roles": ["viewer"]},
        {"tenant_admin"},
    ),
    ("GET", "/api/v1/api-keys", None, {"tenant_admin"}),
    ("GET", "/api/v1/audit-logs", None, {"tenant_admin", "manager"}),
    ("PATCH", "/api/v1/tenant", {"settings": {"theme": "dark"}}, {"tenant_admin"}),
    ("GET", "/api/v1/admin/tenants", None, set()),
    ("GET", "/api/v1/admin/audit-logs", None, set()),
]
ROLES = ["tenant_admin", "manager", "engineer", "viewer"]


@pytest.mark.parametrize("role", ROLES)
async def test_rbac_matrix(client: AsyncClient, factory: Factory, role: str) -> None:
    actor = await factory.user(await factory.tenant(), [role])
    for method, path, body, allowed in GUARDED:
        if isinstance(body, dict) and "email" in body:
            body = {**body, "email": body["email"].format(n=uuid.uuid4().hex[:8])}
        resp = await client.request(method, path, json=body, headers=actor.headers)
        expected_ok = role in allowed
        assert (resp.status_code < 400) == expected_ok, (
            f"{role} {method} {path} -> {resp.status_code} {resp.text}"
        )
        if not expected_ok:
            assert resp.status_code == 403


async def test_last_tenant_admin_is_protected(client: AsyncClient, factory: Factory) -> None:
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    other_admin = await factory.user(tenant, ["tenant_admin"], login=False)

    demote = await client.put(
        f"/api/v1/users/{other_admin.user_id}/roles", json={"roles": ["viewer"]}, headers=admin.headers
    )
    assert demote.status_code == 200
    assert demote.json()["roles"] == ["viewer"]

    # `admin` is now the only admin; disabling yourself is refused, and so is demoting the last admin.
    assert (
        await client.post(f"/api/v1/users/{admin.user_id}/disable", headers=admin.headers)
    ).status_code == 403
    assert (
        await client.put(
            f"/api/v1/users/{admin.user_id}/roles", json={"roles": ["viewer"]}, headers=admin.headers
        )
    ).status_code == 409


async def test_unknown_role_rejected(client: AsyncClient, factory: Factory) -> None:
    admin = await factory.user(await factory.tenant(), ["tenant_admin"])
    resp = await client.post(
        "/api/v1/users/invite",
        json={"email": "r@example.com", "full_name": "R", "roles": ["god_mode"]},
        headers=admin.headers,
    )
    assert resp.status_code == 422


async def test_duplicate_email_across_tenants_conflicts(client: AsyncClient, factory: Factory) -> None:
    other = await factory.user(await factory.tenant(), ["viewer"], login=False)
    admin = await factory.user(await factory.tenant(), ["tenant_admin"])
    resp = await client.post(
        "/api/v1/users/invite",
        json={"email": other.email, "full_name": "Dup", "roles": ["viewer"]},
        headers=admin.headers,
    )
    assert resp.status_code == 409


async def test_super_admin_creates_tenant_with_admin_invite(
    client: AsyncClient, factory: Factory, emails: RecordingEmailSender
) -> None:
    super_admin = await factory.user(None, platform_roles=["super_admin"])
    slug = f"acme-{uuid.uuid4().hex[:6]}"
    admin_email = f"admin-{slug}@example.com"

    created = await client.post(
        "/api/v1/admin/tenants",
        json={"name": "ACME EPC", "slug": slug, "admin": {"email": admin_email, "full_name": "ACME Admin"}},
        headers=super_admin.headers,
    )
    assert created.status_code == 201, created.text
    tenant_id = created.json()["id"]

    dup = await client.post(
        "/api/v1/admin/tenants",
        json={"name": "Again", "slug": slug, "admin": {"email": f"x-{admin_email}", "full_name": "X"}},
        headers=super_admin.headers,
    )
    assert dup.status_code == 409

    token = re.search(r"token=([\w-]+)", emails.last_to(admin_email).body).group(1)  # type: ignore[union-attr]
    accepted = await client.post(
        "/api/v1/auth/invitations/accept", json={"token": token, "password": "Adm1n-Passw0rd!"}
    )
    assert accepted.status_code == 200
    me = accepted.json()["user"]
    assert me["tenant"]["id"] == tenant_id and me["roles"] == ["tenant_admin"]

    # Suspending the tenant locks its users out.
    assert (
        await client.post(f"/api/v1/admin/tenants/{tenant_id}/suspend", headers=super_admin.headers)
    ).status_code == 200
    blocked = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {accepted.json()['access_token']}"}
    )
    assert blocked.status_code == 403


async def test_audit_log_records_actions_per_tenant(client: AsyncClient, factory: Factory) -> None:
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    other_tenant_admin = await factory.user(await factory.tenant(), ["tenant_admin"])

    await client.patch("/api/v1/tenant", json={"name": "Renamed Co"}, headers=admin.headers)
    logs = await client.get("/api/v1/audit-logs", headers=admin.headers)
    assert logs.status_code == 200
    actions = [e["action"] for e in logs.json()["items"]]
    assert "tenant.settings_updated" in actions and "auth.login" in actions
    assert all(e["tenant_id"] == str(tenant) for e in logs.json()["items"])

    others = await client.get("/api/v1/audit-logs", headers=other_tenant_admin.headers)
    assert all(e["tenant_id"] == str(other_tenant_admin.tenant_id) for e in others.json()["items"])


async def test_cursor_pagination(client: AsyncClient, factory: Factory) -> None:
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    for _ in range(4):
        await factory.user(tenant, ["viewer"], login=False)

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/api/v1/users", params=params, headers=admin.headers)).json()
        seen += [u["id"] for u in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 5 == len(set(seen))
