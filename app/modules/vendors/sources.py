"""The source register (SOW 24.4): every place the knowledge base's facts came from, grouped by website domain (or by
kind for documents, Excel imports, manual entry and AI), with how much rests on it and how reliable it is judged.

Curators set a group's reliability; it shows beside each source on the vendor page. Facts themselves are never
changed here: a wrong fact is corrected through a proposal, so the review trail stays intact.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Text, case, cast, func, select, update

from app.api.deps import SessionDep, require
from app.domain.enums import SourceKind
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.audit.service import record_audit
from app.modules.vendors.models import FactSource, VendorSource

router = APIRouter(tags=["vendors"])
Curator = Annotated[Principal, Depends(require(P.KNOWLEDGE_APPROVE))]

# The host of a web source, without "www." ("https://www.flowserve.com/pumps" -> "flowserve.com").
_HOST = func.lower(func.substring(VendorSource.url, r"^[A-Za-z][A-Za-z0-9+.-]*://(?:www\.)?([^/:?#]+)"))


class SourceGroup(BaseModel):
    key: str  # a website domain, or the source kind for non-web sources
    kind: SourceKind
    sources: int  # source records (pages, documents)
    vendors: int
    facts: int  # facts citing them
    last_retrieved: datetime | None
    reliability: Decimal | None  # the group's rating (0-1); None when not rated or mixed


class SourceRegister(BaseModel):
    groups: list[SourceGroup]
    total: int


class ReliabilityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: SourceKind
    key: str = Field(min_length=1, max_length=255)
    reliability: Decimal | None = Field(default=None, ge=0, le=1)


class ReliabilityResult(BaseModel):
    updated: int


def _group_filter(kind: SourceKind, key: str) -> Any:
    if kind == SourceKind.WEB:
        return (VendorSource.kind == SourceKind.WEB) & (func.coalesce(_HOST, "") == key.lower())
    return VendorSource.kind == kind


@router.get("/admin/sources", response_model=SourceRegister)
async def source_register(
    _: Curator,
    session: SessionDep,
    q: str = "",
    kind: Literal["web", "document", "excel", "manual", "ai"] | None = None,
    limit: int = 200,
) -> SourceRegister:
    """Sources grouped by domain (web) or kind, most-cited first."""
    facts = select(FactSource.source_id, func.count().label("n")).group_by(FactSource.source_id).subquery()
    group_key = case(
        (VendorSource.kind == SourceKind.WEB, func.coalesce(_HOST, "")), else_=cast(VendorSource.kind, Text)
    )
    stmt = (
        select(
            group_key.label("key"),
            VendorSource.kind,
            func.count(VendorSource.id),
            func.count(func.distinct(VendorSource.vendor_id)),
            func.coalesce(func.sum(facts.c.n), 0),
            func.max(VendorSource.retrieved_at),
            func.min(VendorSource.reliability),
            func.max(VendorSource.reliability),
        )
        .outerjoin(facts, facts.c.source_id == VendorSource.id)
        .group_by(group_key, VendorSource.kind)
    )
    if kind:
        stmt = stmt.where(VendorSource.kind == SourceKind(kind))
    if q.strip():
        stmt = stmt.where(group_key.ilike(f"%{q.strip().lower()}%"))
    rows = (await session.execute(stmt)).all()
    groups = [
        SourceGroup(
            key=k or "(no address)",
            kind=sk,
            sources=int(n),
            vendors=int(v),
            facts=int(f),
            last_retrieved=last,
            reliability=lo if lo == hi else None,
        )
        for k, sk, n, v, f, last, lo, hi in rows
    ]
    groups.sort(key=lambda g: (-g.facts, -g.sources, g.key))
    return SourceRegister(groups=groups[: max(1, min(limit, 1000))], total=len(groups))


@router.put("/admin/sources/reliability", response_model=ReliabilityResult)
async def set_reliability(
    body: ReliabilityUpdate, principal: Curator, session: SessionDep
) -> ReliabilityResult:
    """Rates every source in a group (a domain, or a kind). Audited; the facts themselves are untouched."""
    key = body.key.lower() if body.kind == SourceKind.WEB else body.kind.value
    result = await session.execute(
        update(VendorSource)
        .where(_group_filter(body.kind, key))
        .values(reliability=body.reliability)
        .execution_options(synchronize_session=False)
    )
    count = int(getattr(result, "rowcount", 0) or 0)
    record_audit(
        session,
        "sources.reliability_set",
        principal=principal,
        tenant_id=None,
        entity_type="source_group",
        entity_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{body.kind.value}:{key}"),
        changes={"kind": body.kind.value, "key": key, "reliability": str(body.reliability), "sources": count},
    )
    await session.commit()
    return ReliabilityResult(updated=count)
