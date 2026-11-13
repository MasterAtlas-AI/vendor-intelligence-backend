import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import Forbidden, NotFound, ValidationFailed
from app.core.security import generate_api_key
from app.domain.permissions import API_KEY_ALLOWED_SCOPES
from app.domain.principal import Principal
from app.modules.api_keys.models import ApiKey
from app.modules.api_keys.schemas import ApiKeyCreate, ApiKeyUpdate
from app.modules.audit.service import record_audit


class ApiKeyService:
    def __init__(self, session: AsyncSession, principal: Principal, settings: Settings) -> None:
        self.s = session
        self.principal = principal
        self.settings = settings

    @property
    def tenant_id(self) -> uuid.UUID:
        if self.principal.tenant_id is None:
            raise Forbidden(
                "Select a tenant (X-Act-As-Tenant) to manage its API keys", code="tenant_required"
            )
        return self.principal.tenant_id

    async def list_keys(self) -> list[ApiKey]:
        return list(
            await self.s.scalars(
                select(ApiKey).where(ApiKey.tenant_id == self.tenant_id).order_by(ApiKey.created_at.desc())
            )
        )

    def _check_scopes(self, requested: list[str]) -> list[str]:
        scopes = sorted(set(requested))
        not_allowed = [s for s in scopes if s not in API_KEY_ALLOWED_SCOPES]
        # A key can never hold more than its creator: prevents privilege escalation through integrations.
        not_held = [s for s in scopes if not self.principal.has(s)]
        if not_allowed or not_held:
            errors = [
                {"field": "scopes", "message": f"Scope not allowed for API keys: {s}"} for s in not_allowed
            ]
            errors += [{"field": "scopes", "message": f"You do not hold: {s}"} for s in not_held]
            raise ValidationFailed("Invalid scopes", errors=errors)
        return scopes

    async def create(self, body: ApiKeyCreate) -> tuple[ApiKey, str]:
        scopes = self._check_scopes(body.scopes)
        generated = generate_api_key(self.settings.api_key_prefix)
        key = ApiKey(
            tenant_id=self.tenant_id,
            name=body.name,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=scopes,
            ip_allowlist=body.ip_allowlist,
            expires_at=body.expires_at,
            rate_limit_per_minute=body.rate_limit_per_minute,
        )
        self.s.add(key)
        await self.s.flush()
        record_audit(
            self.s,
            "api_key.created",
            principal=self.principal,
            entity_type="api_key",
            entity_id=key.id,
            changes={"name": key.name, "scopes": scopes, "prefix": key.prefix},
        )
        await self.s.commit()
        return key, generated.key

    async def _own(self, key_id: uuid.UUID) -> ApiKey:
        key = await self.s.get(ApiKey, key_id)
        if key is None or key.tenant_id != self.tenant_id:
            raise NotFound("API key not found")
        return key

    async def update(self, key_id: uuid.UUID, body: ApiKeyUpdate) -> ApiKey:
        """Renames a key or changes its scopes, expiry, IP allowlist or rate limit. Takes effect on the key's next
        call. A revoked key can't be changed (create a new one)."""
        key = await self._own(key_id)
        if key.revoked_at is not None:
            raise ValidationFailed("This key is revoked; create a new key instead")
        sent = body.model_fields_set
        changes: dict[str, object] = {}
        if body.name is not None and body.name != key.name:
            changes["name"] = key.name = body.name.strip()
        if body.scopes is not None:
            scopes = self._check_scopes(body.scopes)
            if scopes != sorted(key.scopes):
                changes["scopes"] = key.scopes = scopes
        if "expires_at" in sent and body.expires_at != key.expires_at:
            key.expires_at = body.expires_at
            changes["expires_at"] = body.expires_at.isoformat() if body.expires_at else None
        if body.ip_allowlist is not None:
            changes["ip_allowlist"] = key.ip_allowlist = body.ip_allowlist
        if body.rate_limit_per_minute is not None and body.rate_limit_per_minute != key.rate_limit_per_minute:
            changes["rate_limit_per_minute"] = key.rate_limit_per_minute = body.rate_limit_per_minute
        if changes:
            record_audit(
                self.s,
                "api_key.updated",
                principal=self.principal,
                entity_type="api_key",
                entity_id=key.id,
                changes={"prefix": key.prefix, **changes},
            )
            await self.s.commit()
            await self.s.refresh(key)
        return key

    async def delete(self, key_id: uuid.UUID) -> None:
        """Removes a key for good (it stops working at once). The audit log keeps a record of it."""
        key = await self._own(key_id)
        record_audit(
            self.s,
            "api_key.deleted",
            principal=self.principal,
            entity_type="api_key",
            entity_id=key.id,
            changes={"prefix": key.prefix, "name": key.name, "scopes": list(key.scopes)},
        )
        await self.s.delete(key)
        await self.s.commit()

    async def revoke(self, key_id: uuid.UUID) -> None:
        key = await self._own(key_id)
        if key.revoked_at is None:
            key.revoked_at = datetime.now(UTC)
            record_audit(
                self.s,
                "api_key.revoked",
                principal=self.principal,
                entity_type="api_key",
                entity_id=key.id,
                changes={"prefix": key.prefix},
            )
            await self.s.commit()
