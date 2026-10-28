import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import Conflict, Forbidden, NotFound, ValidationFailed
from app.core.pagination import paginate
from app.core.security import generate_opaque_token, sha256_hex
from app.domain.enums import UserStatus, UserTokenPurpose
from app.domain.permissions import P
from app.domain.principal import Principal
from app.infra.email import EmailSender, invite_email
from app.modules.audit.service import diff, record_audit
from app.modules.auth.service import AuthService
from app.modules.tenants.models import Tenant
from app.modules.users.models import Permission, Role, User, UserRole, UserToken

TENANT_ADMIN = "tenant_admin"


async def resolve_roles(session: AsyncSession, tenant_id: uuid.UUID, keys: list[str]) -> list[Role]:
    roles = list(
        await session.scalars(
            select(Role).where(Role.key.in_(keys), or_(Role.tenant_id.is_(None), Role.tenant_id == tenant_id))
        )
    )
    missing = set(keys) - {r.key for r in roles}
    if missing:
        raise ValidationFailed(
            "Unknown role",
            errors=[{"field": "roles", "message": f"Unknown role: {k}"} for k in sorted(missing)],
        )
    return roles


async def create_invited_user(
    session: AsyncSession,
    *,
    tenant: Tenant,
    email: str,
    full_name: str,
    role_keys: list[str],
    settings: Settings,
) -> tuple[User, str]:
    """Creates an INVITED user with roles and a single-use invite token. Caller commits and sends the email."""
    roles = await resolve_roles(session, tenant.id, role_keys)
    user = User(tenant_id=tenant.id, email=email, full_name=full_name, status=UserStatus.INVITED)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Unique across all tenants; RLS hides other tenants' rows, so the constraint is the only reliable check.
        raise Conflict("A user with this email already exists", code="duplicate") from exc
    session.add_all(UserRole(user_id=user.id, role_id=r.id, tenant_id=tenant.id) for r in roles)
    raw = generate_opaque_token()
    session.add(
        UserToken(
            user_id=user.id,
            tenant_id=tenant.id,
            purpose=UserTokenPurpose.INVITE,
            token_hash=sha256_hex(raw),
            expires_at=datetime.now(UTC) + timedelta(hours=settings.invite_token_ttl_hours),
        )
    )
    await session.flush()
    await session.refresh(user, ["roles"])
    return user, raw


class UserService:
    def __init__(
        self, session: AsyncSession, principal: Principal, settings: Settings, email: EmailSender
    ) -> None:
        self.s = session
        self.principal = principal
        self.settings = settings
        self.email = email

    @property
    def tenant_id(self) -> uuid.UUID:
        if self.principal.tenant_id is None:
            raise Forbidden("Select a tenant (X-Act-As-Tenant) to manage its users", code="tenant_required")
        return self.principal.tenant_id

    async def list_page(
        self, *, q: str | None, status: UserStatus | None, limit: int, cursor: str | None
    ) -> tuple[list[User], str | None]:
        stmt: Select[User] = select(User).where(User.tenant_id == self.tenant_id)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(User.email.ilike(like), User.full_name.ilike(like)))
        if status:
            stmt = stmt.where(User.status == status)
        rows, nxt = await paginate(
            self.s, stmt, created_at_col=User.created_at, id_col=User.id, limit=limit, cursor=cursor
        )
        return list(rows), nxt

    async def get(self, user_id: uuid.UUID) -> User:
        user = await self.s.get(User, user_id)
        if user is None or user.tenant_id != self.tenant_id:
            raise NotFound("User not found")
        return user

    async def invite(self, email: str, full_name: str, role_keys: list[str]) -> User:
        tenant = await self.s.get(Tenant, self.tenant_id)
        assert tenant is not None
        user, raw = await create_invited_user(
            self.s,
            tenant=tenant,
            email=email,
            full_name=full_name,
            role_keys=role_keys,
            settings=self.settings,
        )
        record_audit(
            self.s,
            "user.invited",
            principal=self.principal,
            entity_type="user",
            entity_id=user.id,
            changes={"email": email, "roles": role_keys},
        )
        await self.s.commit()
        await self.email.send(
            invite_email(email, tenant.name, f"{self.settings.frontend_url}/accept-invite?token={raw}")
        )
        return user

    async def update(self, user_id: uuid.UUID, full_name: str | None) -> User:
        is_self = user_id == self.principal.user_id
        if not is_self and not self.principal.has(P.USER_MANAGE):
            raise Forbidden("Missing permission: user:manage")
        user = await self.get(user_id)
        before = {"full_name": user.full_name}
        if full_name is not None:
            user.full_name = full_name
        changes = diff(before, {"full_name": user.full_name})
        if changes:
            record_audit(
                self.s,
                "user.updated",
                principal=self.principal,
                entity_type="user",
                entity_id=user.id,
                changes=changes,
            )
        await self.s.commit()
        return user

    async def set_status(self, user_id: uuid.UUID, status: UserStatus) -> User:
        if user_id == self.principal.user_id:
            raise Forbidden("You cannot change your own status")
        user = await self.get(user_id)
        if status == UserStatus.DISABLED and TENANT_ADMIN in user.role_keys:
            await self._ensure_other_admin(user.id)
        if status == UserStatus.ACTIVE and user.password_hash is None:
            raise ValidationFailed("The user has not accepted the invitation yet")
        before = user.status
        user.status = status
        if status == UserStatus.DISABLED:
            await AuthService.revoke_user_sessions(self.s, [user.id])
        record_audit(
            self.s,
            f"user.{'disabled' if status == UserStatus.DISABLED else 'enabled'}",
            principal=self.principal,
            entity_type="user",
            entity_id=user.id,
            changes={"status": {"from": before, "to": status}},
        )
        await self.s.commit()
        return user

    async def set_roles(self, user_id: uuid.UUID, role_keys: list[str]) -> User:
        user = await self.get(user_id)
        roles = await resolve_roles(self.s, self.tenant_id, role_keys)
        before = user.role_keys
        if TENANT_ADMIN in before and TENANT_ADMIN not in role_keys:
            await self._ensure_other_admin(user.id)
        await self.s.execute(delete(UserRole).where(UserRole.user_id == user.id))
        self.s.add_all(UserRole(user_id=user.id, role_id=r.id, tenant_id=self.tenant_id) for r in roles)
        user.perms_version += 1
        record_audit(
            self.s,
            "user.roles_changed",
            principal=self.principal,
            entity_type="user",
            entity_id=user.id,
            changes={"roles": {"from": before, "to": sorted(role_keys)}},
        )
        await self.s.commit()
        await self.s.refresh(user, ["roles"])
        return user

    async def _ensure_other_admin(self, excluding: uuid.UUID) -> None:
        count = await self.s.scalar(
            select(func.count())
            .select_from(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                User.tenant_id == self.tenant_id,
                User.id != excluding,
                User.status == UserStatus.ACTIVE,
                Role.key == TENANT_ADMIN,
            )
        )
        if not count:
            raise Conflict("The tenant must keep at least one active Tenant Admin", code="last_admin")

    async def list_roles(self) -> list[Role]:
        return list(
            await self.s.scalars(
                select(Role)
                .where(or_(Role.tenant_id.is_(None), Role.tenant_id == self.principal.tenant_id))
                .order_by(Role.is_system.desc(), Role.name)
            )
        )

    async def list_permissions(self) -> list[Permission]:
        return list(await self.s.scalars(select(Permission).order_by(Permission.key)))
