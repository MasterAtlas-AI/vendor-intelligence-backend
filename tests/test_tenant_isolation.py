"""Phase 1 exit criterion: a tenant can never read or write another tenant's rows — even through a query
that forgets the tenant filter, because Postgres RLS is enforced for the application role."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.container import Container
from app.db.session import open_session
from app.domain.enums import UserStatus
from app.domain.principal import TenantContext
from app.modules.users.models import User
from tests.conftest import Factory


async def test_api_never_returns_other_tenants_users(client: AsyncClient, factory: Factory) -> None:
    tenant_a, tenant_b = await factory.tenant(), await factory.tenant()
    admin_a = await factory.user(tenant_a, ["tenant_admin"])
    user_b = await factory.user(tenant_b, ["viewer"], login=False)

    listed = await client.get("/api/v1/users", headers=admin_a.headers)
    assert listed.status_code == 200
    ids = {u["id"] for u in listed.json()["items"]}
    assert str(admin_a.user_id) in ids
    assert str(user_b.user_id) not in ids

    for path in (f"/api/v1/users/{user_b.user_id}", f"/api/v1/users/{user_b.user_id}/disable"):
        method = client.get if path.endswith(str(user_b.user_id)) else client.post
        resp = await method(path, headers=admin_a.headers)
        assert resp.status_code == 404


async def test_rls_hides_rows_from_unfiltered_queries(container: Container, factory: Factory) -> None:
    tenant_a, tenant_b = await factory.tenant(), await factory.tenant()
    await factory.user(tenant_a, ["viewer"], login=False)
    await factory.user(tenant_b, ["viewer"], login=False)

    async with open_session(container.sessionmaker, TenantContext(tenant_a)) as s:
        # Deliberately no WHERE tenant_id = ...
        tenants_seen = set(await s.scalars(select(User.tenant_id).distinct()))
        assert tenants_seen == {tenant_a}
        raw = await s.scalar(text("SELECT count(*) FROM users WHERE tenant_id = :b"), {"b": tenant_b})
        assert raw == 0


async def test_rls_blocks_cross_tenant_writes(container: Container, factory: Factory) -> None:
    tenant_a, tenant_b = await factory.tenant(), await factory.tenant()
    async with open_session(container.sessionmaker, TenantContext(tenant_a)) as s:
        s.add(
            User(
                tenant_id=tenant_b,
                email=f"x-{uuid.uuid4().hex[:8]}@example.com",
                full_name="Intruder",
                status=UserStatus.INVITED,
                platform_roles=[],
            )
        )
        with pytest.raises(DBAPIError, match="row-level security"):
            await s.flush()


async def test_session_without_tenant_sees_nothing(container: Container, factory: Factory) -> None:
    await factory.user(await factory.tenant(), ["viewer"], login=False)
    async with open_session(container.sessionmaker, TenantContext(tenant_id=None)) as s:
        assert await s.scalar(select(func.count()).select_from(User)) == 0


async def test_app_role_cannot_tamper_with_audit_log(container: Container) -> None:
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        with pytest.raises(DBAPIError, match="permission denied"):
            await s.execute(text("DELETE FROM audit_logs"))


async def test_impersonation_is_limited_to_super_admins(client: AsyncClient, factory: Factory) -> None:
    tenant_a, tenant_b = await factory.tenant(), await factory.tenant()
    admin_a = await factory.user(tenant_a, ["tenant_admin"])
    user_b = await factory.user(tenant_b, ["viewer"], login=False)
    super_admin = await factory.user(None, platform_roles=["super_admin"])

    denied = await client.get("/api/v1/users", headers={**admin_a.headers, "X-Act-As-Tenant": str(tenant_b)})
    assert denied.status_code == 403

    acting = await client.get(
        "/api/v1/users", headers={**super_admin.headers, "X-Act-As-Tenant": str(tenant_b)}
    )
    assert acting.status_code == 200
    assert {u["id"] for u in acting.json()["items"]} == {str(user_b.user_id)}
