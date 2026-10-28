import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import client_ip_var, request_id_var, user_agent_var
from app.domain.enums import ActorType
from app.domain.principal import Principal
from app.modules.audit.models import AuditLog

_UNSET: Any = object()


def record_audit(
    session: AsyncSession,
    action: str,
    *,
    principal: Principal | None = None,
    actor_type: ActorType | None = None,
    actor_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = _UNSET,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    changes: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Adds an audit row to the caller's transaction, so it commits (or rolls back) with the change itself."""
    meta = dict(metadata or {})
    if principal and principal.impersonating:
        meta["impersonated_by_platform"] = True
    entry = AuditLog(
        tenant_id=(principal.tenant_id if principal else None) if tenant_id is _UNSET else tenant_id,
        actor_type=actor_type or (principal.actor_type if principal else ActorType.SYSTEM),
        actor_id=actor_id or (principal.id if principal else None),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        changes=changes or {},
        metadata_=meta,
        ip=client_ip_var.get(),
        user_agent=(user_agent_var.get() or "")[:500] or None,
        request_id=request_id_var.get(),
    )
    session.add(entry)
    return entry


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """`{"field": {"from": x, "to": y}}` for fields whose value changed."""
    return {k: {"from": before.get(k), "to": v} for k, v in after.items() if before.get(k) != v}
