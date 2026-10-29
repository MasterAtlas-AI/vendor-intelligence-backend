import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.ai.service import AINotAvailable
from app.api.deps import ContainerDep, SessionDep, require
from app.db.session import open_session
from app.discovery.capabilities import CapabilityRequest
from app.domain.permissions import P
from app.domain.principal import Principal, TenantContext
from app.modules.discovery.router import _start_capability_job
from app.modules.quality.service import ISSUES, dashboard, rescore, stale_with_website

router = APIRouter(tags=["data quality"])

Curator = Annotated[Principal, Depends(require(P.KNOWLEDGE_APPROVE))]


class IssueCount(BaseModel):
    key: str
    label: str
    count: int


class QualityVendor(BaseModel):
    vendor_id: uuid.UUID
    name: str
    country: str | None
    score: float
    issues: list[str]


class QualityDashboard(BaseModel):
    vendors: int
    avg_score: float
    distribution: dict[str, int]  # low (<0.4) / medium / high (>=0.7)
    issues: list[IssueCount]
    worst: list[QualityVendor]


class RescoreResult(BaseModel):
    updated: int


class ReverifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, ge=1, le=50)  # each re-check reads the website with AI (a few cents)


class ReverifyResult(BaseModel):
    queued: int
    job_ids: list[uuid.UUID]


@router.get("/admin/data-quality", response_model=QualityDashboard)
async def get_dashboard(
    _: Curator,
    c: ContainerDep,
    issue: Annotated[str | None, Query(pattern="^(" + "|".join(ISSUES) + ")$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> QualityDashboard:
    """Knowledge-base quality: score distribution, how many vendors have each issue, and the weakest vendors."""
    async with open_session(c.sessionmaker, TenantContext.platform()) as s:
        data = await dashboard(s, issue=issue, limit=limit)
    return QualityDashboard(
        vendors=data["vendors"], avg_score=data["avg_score"], distribution=data["distribution"],
        issues=[IssueCount(**i) for i in data["issues"]],
        worst=[QualityVendor(**vars(r)) for r in data["worst"]],
    )  # fmt: skip


@router.post("/admin/data-quality/rescore", response_model=RescoreResult)
async def rescore_all(_: Curator, c: ContainerDep) -> RescoreResult:
    """Recompute every vendor's score now (the maintenance loop also does this)."""
    async with open_session(c.sessionmaker, TenantContext.platform()) as s:
        updated = await rescore(s)
        await s.commit()
    return RescoreResult(updated=updated)


@router.post("/admin/data-quality/reverify", response_model=ReverifyResult)
async def reverify_stale(
    body: ReverifyRequest, principal: Curator, session: SessionDep, c: ContainerDep
) -> ReverifyResult:
    """Re-read the websites of the stalest vendors (a full AI refresh each, proposals go to review as usual)."""
    if not c.ai.configured:
        raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
    async with open_session(c.sessionmaker, TenantContext.platform()) as s:
        vendor_ids = await stale_with_website(s, body.limit)
    job_ids = []
    for vid in vendor_ids:
        accepted = await _start_capability_job(
            "refresh", vid, CapabilityRequest(), principal, session, c, None
        )
        job_ids.append(accepted.job_id)
    return ReverifyResult(queued=len(job_ids), job_ids=job_ids)
