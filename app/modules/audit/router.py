import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Select, select

from app.api.deps import SessionDep, require
from app.core.exceptions import Forbidden
from app.core.pagination import CursorPage, paginate
from app.domain.enums import ActorType
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.audit.models import AuditLog

router = APIRouter(tags=["audit"])


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    id: uuid.UUID
    created_at: datetime
    tenant_id: uuid.UUID | None
    actor_type: ActorType
    actor_id: uuid.UUID | None
    action: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    changes: dict[str, Any]
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    ip: str | None
    request_id: str | None

    @field_validator("ip", mode="before")
    @classmethod
    def _ip(cls, v: object) -> str | None:
        # asyncpg returns ipaddress objects for INET; "1.2.3.4/32" -> "1.2.3.4"
        return None if v is None else str(v).removesuffix("/32").removesuffix("/128")


class AuditFilters(BaseModel):
    actor_id: uuid.UUID | None = None
    action: str | None = Field(default=None, max_length=100)
    entity_type: str | None = Field(default=None, max_length=64)
    entity_id: uuid.UUID | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None


def _apply(stmt: Select[AuditLog], f: AuditFilters) -> Select[AuditLog]:
    if f.actor_id:
        stmt = stmt.where(AuditLog.actor_id == f.actor_id)
    if f.action:
        # "user." matches every user.* action
        stmt = (
            stmt.where(AuditLog.action.startswith(f.action))
            if f.action.endswith(".")
            else stmt.where(AuditLog.action == f.action)
        )
    if f.entity_type:
        stmt = stmt.where(AuditLog.entity_type == f.entity_type)
    if f.entity_id:
        stmt = stmt.where(AuditLog.entity_id == f.entity_id)
    if f.from_:
        stmt = stmt.where(AuditLog.created_at >= f.from_)
    if f.to:
        stmt = stmt.where(AuditLog.created_at < f.to)
    return stmt


async def _page(
    session: SessionDep, stmt: Select[AuditLog], limit: int, cursor: str | None
) -> CursorPage[AuditLogRead]:
    rows, nxt = await paginate(
        session, stmt, created_at_col=AuditLog.created_at, id_col=AuditLog.id, limit=limit, cursor=cursor
    )
    return CursorPage(
        items=[AuditLogRead.model_validate(r) for r in rows], next_cursor=nxt, has_more=nxt is not None
    )


@router.get("/audit-logs", response_model=CursorPage[AuditLogRead])
async def list_tenant_audit_logs(
    principal: Annotated[Principal, Depends(require(P.AUDIT_READ))],
    session: SessionDep,
    filters: Annotated[AuditFilters, Query()],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> CursorPage[AuditLogRead]:
    if principal.tenant_id is None:
        raise Forbidden("No tenant selected", code="tenant_required")
    stmt = _apply(select(AuditLog).where(AuditLog.tenant_id == principal.tenant_id), filters)
    return await _page(session, stmt, limit, cursor)


@router.get("/admin/audit-logs", response_model=CursorPage[AuditLogRead])
async def list_platform_audit_logs(
    _: Annotated[Principal, Depends(require(P.TENANT_MANAGE))],
    session: SessionDep,
    filters: Annotated[AuditFilters, Query()],
    tenant_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> CursorPage[AuditLogRead]:
    stmt = select(AuditLog)
    if tenant_id:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    return await _page(session, _apply(stmt, filters), limit, cursor)
