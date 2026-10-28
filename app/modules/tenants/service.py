import uuid
from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import Conflict, Forbidden, NotFound
from app.core.pagination import paginate
from app.domain.enums import TenantStatus
from app.domain.principal import Principal
from app.infra.email import EmailSender, invite_email
from app.modules.audit.service import diff, record_audit
from app.modules.auth.service import AuthService
from app.modules.tenants.models import Tenant
from app.modules.tenants.schemas import TenantAdminUpdate, TenantCreate, TenantSelfUpdate
from app.modules.users.models import User
from app.modules.users.service import create_invited_user


class TenantService:
    def __init__(
        self, session: AsyncSession, principal: Principal, settings: Settings, email: EmailSender
    ) -> None:
        self.s = session
        self.principal = principal
        self.settings = settings
        self.email = email

    # ---------------------------------------------------------- platform administration

    async def list_page(
        self, *, q: str | None, status: TenantStatus | None, limit: int, cursor: str | None
    ) -> tuple[list[Tenant], str | None]:
        stmt: Select[Tenant] = select(Tenant)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(Tenant.name.ilike(like), Tenant.slug.ilike(like)))
        if status:
            stmt = stmt.where(Tenant.status == status)
        rows, nxt = await paginate(
            self.s, stmt, created_at_col=Tenant.created_at, id_col=Tenant.id, limit=limit, cursor=cursor
        )
        return list(rows), nxt

    async def get(self, tenant_id: uuid.UUID) -> Tenant:
        tenant = await self.s.get(Tenant, tenant_id)
        if tenant is None:
            raise NotFound("Tenant not found")
        return tenant

    async def create(self, body: TenantCreate) -> Tenant:
        tenant = Tenant(
            name=body.name,
            slug=body.slug,
            plan=body.plan,
            status=body.status,
            trusted_source=body.trusted_source,
            quotas=body.quotas,
            settings={},
        )
        self.s.add(tenant)
        try:
            await self.s.flush()
        except IntegrityError as exc:
            raise Conflict("A tenant with this slug already exists", code="duplicate") from exc
        admin, raw = await create_invited_user(
            self.s,
            tenant=tenant,
            email=body.admin.email,
            full_name=body.admin.full_name,
            role_keys=["tenant_admin"],
            settings=self.settings,
        )
        record_audit(
            self.s,
            "tenant.created",
            principal=self.principal,
            tenant_id=tenant.id,
            entity_type="tenant",
            entity_id=tenant.id,
            changes={"name": tenant.name, "slug": tenant.slug, "admin_email": admin.email},
        )
        await self.s.commit()
        await self.email.send(
            invite_email(admin.email, tenant.name, f"{self.settings.frontend_url}/accept-invite?token={raw}")
        )
        return tenant

    async def update(self, tenant_id: uuid.UUID, body: TenantAdminUpdate) -> Tenant:
        tenant = await self.get(tenant_id)
        return await self._apply(tenant, body.model_dump(exclude_unset=True), "tenant.updated")

    async def set_status(self, tenant_id: uuid.UUID, status: TenantStatus) -> Tenant:
        tenant = await self.get(tenant_id)
        before = tenant.status
        tenant.status = status
        if not status.can_sign_in:
            user_ids = list(await self.s.scalars(select(User.id).where(User.tenant_id == tenant.id)))
            if user_ids:
                await AuthService.revoke_user_sessions(self.s, user_ids)
        record_audit(
            self.s,
            "tenant.status_changed",
            principal=self.principal,
            tenant_id=tenant.id,
            entity_type="tenant",
            entity_id=tenant.id,
            changes={"status": {"from": before, "to": status}},
        )
        await self.s.commit()
        return tenant

    # ---------------------------------------------------------- tenant self-service

    async def get_own(self) -> Tenant:
        if self.principal.tenant_id is None:
            raise Forbidden("No tenant selected", code="tenant_required")
        return await self.get(self.principal.tenant_id)

    async def update_own(self, body: TenantSelfUpdate) -> Tenant:
        tenant = await self.get_own()
        return await self._apply(tenant, body.model_dump(exclude_unset=True), "tenant.settings_updated")

    async def _apply(self, tenant: Tenant, values: dict[str, Any], action: str) -> Tenant:
        before = {k: getattr(tenant, k) for k in values}
        for key, value in values.items():
            setattr(tenant, key, value)
        changes = diff(before, values)
        if changes:
            record_audit(
                self.s,
                action,
                principal=self.principal,
                tenant_id=tenant.id,
                entity_type="tenant",
                entity_id=tenant.id,
                changes=changes,
            )
        await self.s.commit()
        return tenant
