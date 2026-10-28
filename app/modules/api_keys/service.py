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
from app.modules.api_keys.schemas import ApiKeyCreate
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

    async def create(self, body: ApiKeyCreate) -> tuple[ApiKey, str]:
        scopes = sorted(set(body.scopes))
        not_allowed = [s for s in scopes if s not in API_KEY_ALLOWED_SCOPES]
        # A key can never hold more than its creator: prevents privilege escalation through integrations.
        not_held = [s for s in scopes if not self.principal.has(s)]
        if not_allowed or not_held:
            errors = [
                {"field": "scopes", "message": f"Scope not allowed for API keys: {s}"} for s in not_allowed
            ]
            errors += [{"field": "scopes", "message": f"You do not hold: {s}"} for s in not_held]
            raise ValidationFailed("Invalid scopes", errors=errors)

        generated = generate_api_key(self.settings.api_key_prefix)
        key = ApiKey(
            tenant_id=self.tenant_id,
            name=body.name,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=scopes,
            ip_allowlist=body.ip_allowlist,
            expires_at=body.expires_at,
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

    async def revoke(self, key_id: uuid.UUID) -> None:
        key = await self.s.get(ApiKey, key_id)
        if key is None or key.tenant_id != self.tenant_id:
            raise NotFound("API key not found")
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
