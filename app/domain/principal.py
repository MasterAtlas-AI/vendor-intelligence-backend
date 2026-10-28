import uuid
from dataclasses import dataclass, field
from typing import Literal

from app.domain.enums import ActorType


@dataclass(frozen=True)
class TenantContext:
    """What the database session is allowed to see (drives the RLS session settings)."""

    tenant_id: uuid.UUID | None
    is_platform: bool = False

    @classmethod
    def platform(cls) -> "TenantContext":
        return cls(tenant_id=None, is_platform=True)


@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "api_key"]
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    permissions: frozenset[str]
    roles: tuple[str, ...] = ()
    platform_roles: tuple[str, ...] = ()
    email: str | None = None
    impersonating: bool = False
    scopes: tuple[str, ...] = field(default=())

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    @property
    def is_super_admin(self) -> bool:
        return "super_admin" in self.platform_roles

    @property
    def actor_type(self) -> ActorType:
        return ActorType.USER if self.kind == "user" else ActorType.API_KEY

    @property
    def user_id(self) -> uuid.UUID | None:
        return self.id if self.kind == "user" else None

    def tenant_context(self) -> TenantContext:
        # Only an un-impersonating super admin sees across tenants. Curators and impersonating admins
        # are confined to their (possibly empty) tenant, so tenant-private data stays private.
        if self.is_super_admin and self.tenant_id is None:
            return TenantContext.platform()
        return TenantContext(tenant_id=self.tenant_id, is_platform=False)
