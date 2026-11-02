import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.ai.service import AINotAvailable, CallContext
from app.api.deps import ContainerDep, SessionDep, require
from app.db.session import open_session
from app.discovery.capabilities import CapabilityRequest
from app.domain.enums import JobStatus, JobType
from app.domain.permissions import P
from app.domain.principal import Principal, TenantContext
from app.modules.discovery.router import _start_capability_job
from app.modules.jobs.service import JobService
from app.modules.quality.lookup import MAX_VENDORS, Field_, estimate_usd
from app.modules.quality.service import ISSUES, dashboard, rescore, researching, stale_with_website
from app.modules.vendors.models import Vendor

router = APIRouter(tags=["data quality"])

Curator = Annotated[Principal, Depends(require(P.KNOWLEDGE_APPROVE))]


class IssueCount(BaseModel):
    key: str
    label: str
    count: int


class AiCheck(BaseModel):
    field: str  # website, hq_country, description or research
    status: str  # what came of it: proposed, not_found, rejected, failed... (research: its outcome)
    at: datetime


class QualityVendor(BaseModel):
    vendor_id: uuid.UUID
    name: str
    country: str | None
    score: float
    issues: list[str]
    ai_checks: list[AiCheck] = Field(
        description="The latest AI check of each kind run on this vendor, newest first."
    )


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
    hide_checked: Annotated[
        bool, Query(description="Leave out vendors the AI already checked for this list")
    ] = False,
    q: Annotated[str | None, Query(max_length=100, description="Vendor name, legal name or alias")] = None,
) -> QualityDashboard:
    """Knowledge-base quality: score distribution, how many vendors have each issue, and the weakest vendors."""
    async with open_session(c.sessionmaker, TenantContext.platform()) as s:
        data = await dashboard(s, issue=issue, limit=limit, hide_checked=hide_checked, q=q)
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


class LookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_VENDORS)
    field: Field_ = "website"


class LookupAccepted(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    events_url: str
    vendors: int
    estimated_cost_usd: float


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_VENDORS)


class ResearchQueued(BaseModel):
    vendor_id: uuid.UUID
    name: str
    job_id: uuid.UUID | None = None
    skipped: str | None = None  # why no job was started (no website, already being researched)


class ResearchAccepted(BaseModel):
    queued: int
    vendors: list[ResearchQueued]
    estimated_cost_usd: float


@router.post(
    "/admin/data-quality/lookup", response_model=LookupAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def lookup_basics(
    body: LookupRequest, principal: Curator, session: SessionDep, c: ContainerDep
) -> LookupAccepted:
    """Search the web with AI for one missing field (website, headquarters country or description) of the chosen
    vendors, about 1-2 US cents each. Answers that check out go to Approvals; the job's result lists every vendor."""
    if not c.ai.configured:
        raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
    ids = list(dict.fromkeys(body.vendor_ids))
    est = estimate_usd(len(ids), c.settings)
    await c.ai.ensure_budget(CallContext(principal.tenant_id), est)
    job = await JobService(session, principal, c.job_runner.notify).create(
        JobType.VENDOR_LOOKUP,
        {"vendor_ids": [str(v) for v in ids], "field": body.field, "estimate_usd": float(est)},
        None,
    )
    return LookupAccepted(job_id=job.id, status=job.status, events_url=f"{c.settings.api_prefix}/jobs/{job.id}/events",
                          vendors=len(ids), estimated_cost_usd=float(est))  # fmt: skip


@router.post(
    "/admin/data-quality/research", response_model=ResearchAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def research_vendors(
    body: ResearchRequest, principal: Curator, session: SessionDep, c: ContainerDep
) -> ResearchAccepted:
    """Re-read the websites of the chosen vendors with AI (a full refresh each: equipment, services, certifications
    and their sources). New or changed facts go to Approvals. Vendors without a website are skipped."""
    if not c.ai.configured:
        raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
    ids = list(dict.fromkeys(body.vendor_ids))
    async with open_session(c.sessionmaker, TenantContext.platform()) as s:
        busy = await researching(s, ids)
        names = {v.id: (v.display_name, bool(v.website or v.domains))
                 for v in await s.scalars(select(Vendor).where(Vendor.id.in_(ids)))}  # fmt: skip
    out: list[ResearchQueued] = []
    total = Decimal(0)
    for vid in ids:
        name, has_site = names.get(vid, ("Unknown vendor", False))
        if vid not in names:
            out.append(ResearchQueued(vendor_id=vid, name=name, skipped="Vendor not found"))
        elif not has_site:
            out.append(ResearchQueued(vendor_id=vid, name=name, skipped="No website to read; find it first"))
        elif vid in busy:
            out.append(ResearchQueued(vendor_id=vid, name=name, skipped="Already being researched"))
        else:
            accepted = await _start_capability_job(
                "refresh", vid, CapabilityRequest(), principal, session, c, None
            )
            total += Decimal(str(accepted.estimate.typical_cost_usd))
            out.append(ResearchQueued(vendor_id=vid, name=name, job_id=accepted.job_id))
    return ResearchAccepted(queued=sum(1 for v in out if v.job_id), vendors=out,
                            estimated_cost_usd=float(total.quantize(Decimal("0.01"))))  # fmt: skip
