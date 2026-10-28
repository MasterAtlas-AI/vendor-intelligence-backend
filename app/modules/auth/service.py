"""Authentication use-cases.

Runs before any tenant is known (login, refresh, token lookups), so it opens its own platform-mode
sessions. This is the only module allowed to do that on behalf of an unauthenticated caller.
"""

import ipaddress
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, Forbidden, Locked, RateLimited, Unauthenticated, ValidationFailed
from app.core.logging import get_logger
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    constant_time_equals,
    generate_opaque_token,
    hash_password,
    parse_api_key,
    sha256_hex,
    verify_password,
)
from app.db.session import open_session
from app.domain.enums import ActorType, UserStatus, UserTokenPurpose
from app.domain.permissions import permissions_for_platform_roles
from app.domain.principal import Principal, TenantContext
from app.infra.email import password_reset_email
from app.modules.api_keys.models import ApiKey
from app.modules.audit.service import record_audit
from app.modules.auth.models import RefreshToken
from app.modules.auth.schemas import CurrentUser, TenantSummary
from app.modules.tenants.models import Tenant
from app.modules.users.models import User, UserToken

if TYPE_CHECKING:
    from app.core.container import Container

log = get_logger(__name__)


def _tenant_inactive() -> Forbidden:
    return Forbidden("Your organisation's account is not active", code="tenant_suspended")


def _invalid_credentials() -> Unauthenticated:
    return Unauthenticated("Invalid email or password", code="invalid_credentials")


API_KEY_TOUCH_INTERVAL = timedelta(seconds=60)


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_token_id: uuid.UUID
    user: CurrentUser


def validate_password_policy(password: str, *, email: str, min_length: int) -> None:
    problems = []
    if len(password) < min_length:
        problems.append(f"Must be at least {min_length} characters")
    local_part = email.split("@")[0].lower()
    # Very short local parts ("a@x.com") would otherwise forbid almost every password.
    if password.lower() == email.lower() or (len(local_part) >= 4 and local_part in password.lower()):
        problems.append("Must not contain your email address")
    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3:
        problems.append("Must use at least 3 of: lowercase, uppercase, digits, symbols")
    if problems:
        raise ValidationFailed(
            "Password does not meet the policy",
            errors=[{"field": "password", "message": p} for p in problems],
        )


def build_current_user(user: User, tenant: Tenant | None, *, impersonating: bool = False) -> CurrentUser:
    perms = user.tenant_permissions | permissions_for_platform_roles(user.platform_roles)
    return CurrentUser(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        tenant=TenantSummary.model_validate(tenant) if tenant else None,
        roles=user.role_keys,
        platform_roles=list(user.platform_roles),
        permissions=sorted(perms),
        impersonating=impersonating,
    )


class AuthService:
    def __init__(self, container: "Container") -> None:
        self._c = container
        self._s = container.settings

    def _platform_session(self) -> AbstractAsyncContextManager[AsyncSession]:
        return open_session(self._c.sessionmaker, TenantContext.platform())

    # ----------------------------------------------------------------- login / tokens

    async def login(
        self, email: str, password: str, *, ip: str | None, user_agent: str | None
    ) -> IssuedTokens:
        limit = self._s.login_rate_limit_per_minute
        if not (
            await self._c.rate_limiter.hit(f"login:ip:{ip}", limit * 3, 60)
            and await self._c.rate_limiter.hit(f"login:email:{email.lower()}", limit, 60)
        ):
            raise RateLimited("Too many login attempts, try again in a minute", headers={"Retry-After": "60"})

        async with self._platform_session() as s:
            user = await s.scalar(select(User).where(User.email == email))
            if user is None or user.password_hash is None:
                verify_password(DUMMY_PASSWORD_HASH, password)
                raise _invalid_credentials()

            now = datetime.now(UTC)
            if user.locked_until and user.locked_until > now:
                raise Locked("Too many failed attempts; try again later")

            ok, needs_rehash = verify_password(user.password_hash, password)
            if not ok:
                user.failed_logins += 1
                if user.failed_logins >= self._s.max_failed_logins:
                    user.locked_until = now + timedelta(minutes=self._s.lockout_minutes)
                    user.failed_logins = 0
                record_audit(
                    s,
                    "auth.login_failed",
                    actor_type=ActorType.USER,
                    actor_id=user.id,
                    tenant_id=user.tenant_id,
                    entity_type="user",
                    entity_id=user.id,
                    metadata={"locked": user.locked_until is not None and user.locked_until > now},
                )
                await s.commit()
                raise _invalid_credentials()

            if user.status != UserStatus.ACTIVE:
                raise _invalid_credentials()
            tenant = await self._load_active_tenant(s, user)

            user.failed_logins = 0
            user.locked_until = None
            user.last_login_at = now
            if needs_rehash:
                user.password_hash = hash_password(password)
            issued = await self._issue(s, user, tenant, family_id=uuid.uuid4(), ip=ip, user_agent=user_agent)
            record_audit(
                s,
                "auth.login",
                actor_type=ActorType.USER,
                actor_id=user.id,
                tenant_id=user.tenant_id,
                entity_type="user",
                entity_id=user.id,
            )
            await s.commit()
            return issued

    async def refresh(self, raw_token: str | None, *, ip: str | None, user_agent: str | None) -> IssuedTokens:
        if not raw_token:
            raise Unauthenticated("Missing refresh token")
        async with self._platform_session() as s:
            token = await s.scalar(
                select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(raw_token)).with_for_update()
            )
            if token is None:
                raise Unauthenticated("Invalid refresh token")
            now = datetime.now(UTC)
            grace = timedelta(seconds=self._s.refresh_reuse_grace_seconds)
            if token.revoked_at is not None and token.replaced_by_id and now - token.revoked_at < grace:
                # Two tabs refreshed at once: the other request already rotated this token. Not theft.
                raise Unauthenticated("Session was just refreshed elsewhere; retry", code="refresh_conflict")
            if token.revoked_at is not None:
                # A rotated token was replayed: assume theft and kill every session in the family.
                await self._revoke_family(s, token.family_id, now)
                record_audit(
                    s,
                    "auth.refresh_token_reuse",
                    actor_type=ActorType.USER,
                    actor_id=token.user_id,
                    tenant_id=token.tenant_id,
                    entity_type="user",
                    entity_id=token.user_id,
                )
                await s.commit()
                raise Unauthenticated("Refresh token reuse detected; please sign in again")
            if token.expires_at <= now:
                raise Unauthenticated("Refresh token expired", code="token_expired")

            user = await s.get(User, token.user_id)
            if user is None or user.status != UserStatus.ACTIVE:
                raise Unauthenticated("Account is not active")
            tenant = await self._load_active_tenant(s, user)

            issued = await self._issue(
                s, user, tenant, family_id=token.family_id, ip=ip, user_agent=user_agent
            )
            token.revoked_at = now
            token.replaced_by_id = issued.refresh_token_id
            await s.commit()
            return issued

    async def logout(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        async with self._platform_session() as s:
            token = await s.scalar(
                select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(raw_token))
            )
            if token is None:
                return
            await self._revoke_family(s, token.family_id, datetime.now(UTC))
            record_audit(
                s,
                "auth.logout",
                actor_type=ActorType.USER,
                actor_id=token.user_id,
                tenant_id=token.tenant_id,
            )
            await s.commit()

    async def logout_all(self, principal: Principal) -> None:
        if principal.user_id is None:
            raise Forbidden("Only available for user sessions")
        async with self._platform_session() as s:
            await self.revoke_user_sessions(s, [principal.user_id])
            record_audit(
                s, "auth.logout_all", principal=principal, entity_type="user", entity_id=principal.user_id
            )
            await s.commit()

    async def _issue(
        self,
        s: AsyncSession,
        user: User,
        tenant: Tenant | None,
        *,
        family_id: uuid.UUID,
        ip: str | None,
        user_agent: str | None,
    ) -> IssuedTokens:
        access, ttl = self._c.jwt.encode_access_token(
            user_id=user.id,
            tenant_id=user.tenant_id,
            roles=user.role_keys,
            platform_roles=list(user.platform_roles),
            perms_version=user.perms_version,
        )
        raw_refresh = generate_opaque_token()
        record = RefreshToken(
            user_id=user.id,
            tenant_id=user.tenant_id,
            family_id=family_id,
            token_hash=sha256_hex(raw_refresh),
            expires_at=datetime.now(UTC) + timedelta(days=self._s.refresh_token_ttl_days),
            ip=ip,
            user_agent=(user_agent or "")[:500] or None,
        )
        s.add(record)
        await s.flush()
        return IssuedTokens(
            access_token=access,
            expires_in=ttl,
            refresh_token=raw_refresh,
            refresh_token_id=record.id,
            user=build_current_user(user, tenant),
        )

    @staticmethod
    async def _revoke_family(s: AsyncSession, family_id: uuid.UUID, now: datetime) -> None:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    @staticmethod
    async def revoke_user_sessions(s: AsyncSession, user_ids: list[uuid.UUID]) -> None:
        """Revokes all live refresh tokens for the users (caller's session and transaction)."""
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id.in_(user_ids), RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    @staticmethod
    async def _load_active_tenant(s: AsyncSession, user: User) -> Tenant | None:
        if user.tenant_id is None:
            return None
        tenant = await s.get(Tenant, user.tenant_id)
        if tenant is None or not tenant.status.can_sign_in:
            raise _tenant_inactive()
        return tenant

    # ----------------------------------------------------------------- principal resolution

    async def resolve_bearer(self, token: str) -> Principal:
        try:
            claims = self._c.jwt.decode_access_token(token)
        except jwt.ExpiredSignatureError as exc:
            raise Unauthenticated("Access token expired", code="token_expired") from exc
        except jwt.PyJWTError as exc:
            raise Unauthenticated("Invalid access token") from exc

        async with self._platform_session() as s:
            user = await s.get(User, uuid.UUID(claims["sub"]))
            if user is None or user.status != UserStatus.ACTIVE:
                raise Unauthenticated("Account is not active")
            await self._load_active_tenant(s, user)
            perms = user.tenant_permissions | permissions_for_platform_roles(user.platform_roles)
            return Principal(
                kind="user",
                id=user.id,
                tenant_id=user.tenant_id,
                permissions=frozenset(perms),
                roles=tuple(user.role_keys),
                platform_roles=tuple(user.platform_roles),
                email=user.email,
            )

    async def resolve_api_key(self, raw_key: str, client_ip: str | None) -> Principal:
        invalid = Unauthenticated("Invalid API key", code="invalid_api_key")
        prefix = parse_api_key(raw_key, self._s.api_key_prefix)
        if prefix is None:
            raise invalid
        async with self._platform_session() as s:
            key = await s.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
            now = datetime.now(UTC)
            if (
                key is None
                or not constant_time_equals(key.key_hash, sha256_hex(raw_key))
                or key.revoked_at is not None
                or (key.expires_at is not None and key.expires_at <= now)
            ):
                raise invalid
            if key.ip_allowlist and not _ip_allowed(client_ip, key.ip_allowlist):
                raise Forbidden("API key is not allowed from this IP address", code="ip_not_allowed")
            tenant = await s.get(Tenant, key.tenant_id)
            if tenant is None or not tenant.status.can_sign_in:
                raise _tenant_inactive()
            if key.last_used_at is None or now - key.last_used_at > API_KEY_TOUCH_INTERVAL:
                key.last_used_at = now
                await s.commit()
            return Principal(
                kind="api_key",
                id=key.id,
                tenant_id=key.tenant_id,
                permissions=frozenset(key.scopes),
                scopes=tuple(key.scopes),
            )

    async def current_user(self, principal: Principal) -> CurrentUser:
        if principal.kind != "user":
            raise Forbidden("Only available for user sessions")
        async with self._platform_session() as s:
            user = await s.get(User, principal.id)
            assert user is not None
            tenant = await s.get(Tenant, principal.tenant_id) if principal.tenant_id else None
            me = build_current_user(user, tenant, impersonating=principal.impersonating)
            me.permissions = sorted(principal.permissions)
            return me

    # ----------------------------------------------------------------- passwords & invitations

    async def forgot_password(self, email: str) -> None:
        if not await self._c.rate_limiter.hit(f"pwreset:{email.lower()}", 3, 3600):
            return  # silently drop: the response is identical either way
        async with self._platform_session() as s:
            user = await s.scalar(select(User).where(User.email == email))
            if user is None or user.status != UserStatus.ACTIVE:
                return
            raw = generate_opaque_token()
            s.add(
                UserToken(
                    user_id=user.id,
                    tenant_id=user.tenant_id,
                    purpose=UserTokenPurpose.PASSWORD_RESET,
                    token_hash=sha256_hex(raw),
                    expires_at=datetime.now(UTC) + timedelta(minutes=self._s.password_reset_ttl_minutes),
                )
            )
            record_audit(
                s,
                "auth.password_reset_requested",
                actor_type=ActorType.USER,
                actor_id=user.id,
                tenant_id=user.tenant_id,
            )
            await s.commit()
        await self._c.email.send(
            password_reset_email(user.email, f"{self._s.frontend_url}/reset-password?token={raw}")
        )

    async def reset_password(self, raw_token: str, new_password: str) -> None:
        async with self._platform_session() as s:
            _, user = await self._consume_token(s, raw_token, UserTokenPurpose.PASSWORD_RESET)
            validate_password_policy(new_password, email=user.email, min_length=self._s.password_min_length)
            user.password_hash = hash_password(new_password)
            user.failed_logins = 0
            user.locked_until = None
            await self.revoke_user_sessions(s, [user.id])
            record_audit(
                s,
                "auth.password_reset",
                actor_type=ActorType.USER,
                actor_id=user.id,
                tenant_id=user.tenant_id,
                entity_type="user",
                entity_id=user.id,
            )
            await s.commit()

    async def change_password(self, principal: Principal, current: str, new: str) -> None:
        if principal.kind != "user":
            raise Forbidden("Only available for user sessions")
        async with self._platform_session() as s:
            user = await s.get(User, principal.id)
            assert user is not None and user.password_hash is not None
            ok, _ = verify_password(user.password_hash, current)
            if not ok:
                raise ValidationFailed(
                    "Current password is incorrect",
                    errors=[{"field": "current_password", "message": "Incorrect"}],
                )
            validate_password_policy(new, email=user.email, min_length=self._s.password_min_length)
            user.password_hash = hash_password(new)
            await self.revoke_user_sessions(s, [user.id])
            record_audit(
                s, "auth.password_changed", principal=principal, entity_type="user", entity_id=user.id
            )
            await s.commit()

    async def accept_invite(
        self, raw_token: str, full_name: str | None, password: str, *, ip: str | None, user_agent: str | None
    ) -> IssuedTokens:
        async with self._platform_session() as s:
            _, user = await self._consume_token(s, raw_token, UserTokenPurpose.INVITE)
            if user.status != UserStatus.INVITED:
                raise AppError("Invitation already accepted", code="invite_used", status_code=409)
            validate_password_policy(password, email=user.email, min_length=self._s.password_min_length)
            tenant = await self._load_active_tenant(s, user)
            user.password_hash = hash_password(password)
            user.status = UserStatus.ACTIVE
            user.email_verified = True
            if full_name:
                user.full_name = full_name
            user.last_login_at = datetime.now(UTC)
            issued = await self._issue(s, user, tenant, family_id=uuid.uuid4(), ip=ip, user_agent=user_agent)
            record_audit(
                s,
                "user.invite_accepted",
                actor_type=ActorType.USER,
                actor_id=user.id,
                tenant_id=user.tenant_id,
                entity_type="user",
                entity_id=user.id,
            )
            await s.commit()
            return issued

    @staticmethod
    async def _consume_token(
        s: AsyncSession, raw_token: str, purpose: UserTokenPurpose
    ) -> tuple[UserToken, User]:
        token = await s.scalar(
            select(UserToken)
            .where(UserToken.token_hash == sha256_hex(raw_token), UserToken.purpose == purpose)
            .with_for_update()
        )
        now = datetime.now(UTC)
        if token is None or token.used_at is not None or token.expires_at <= now:
            raise AppError("This link is invalid or has expired", code="invalid_token", status_code=400)
        user = await s.get(User, token.user_id)
        if user is None or user.status == UserStatus.DISABLED:
            raise AppError("This link is invalid or has expired", code="invalid_token", status_code=400)
        token.used_at = now
        return token, user


def _ip_allowed(ip: str | None, allowlist: list[str]) -> bool:
    if ip is None:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(str(net), strict=False) for net in allowlist)
