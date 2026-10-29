import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.ai.service import AINotAvailable, CallContext
from app.api.deps import ContainerDep, PrincipalDep, SessionDep, require
from app.core.exceptions import NotFound, ValidationFailed
from app.core.pagination import CursorPage
from app.discovery.capabilities import ALL_SCOPES, CapabilityRequest
from app.discovery.estimate import capability_estimate, estimate
from app.discovery.schemas import DiscoveryEstimate, DiscoveryRequest
from app.domain.enums import ImportRowOutcome, JobStatus, JobType, VendorStatus
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.imports.importer import Taxonomy
from app.modules.imports.schemas import ImportRowPage, ImportRowRead
from app.modules.imports.service import ImportService
from app.modules.jobs.schemas import JobAccepted, JobRead
from app.modules.jobs.service import JobService
from app.modules.vendors.models import Vendor

router = APIRouter(tags=["discovery"])
Discoverer = Annotated[Principal, Depends(require(P.DISCOVERY_RUN))]


class DiscoveryAccepted(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    events_url: str
    estimate: DiscoveryEstimate


async def _validate_codes(session: SessionDep, body: DiscoveryRequest) -> None:
    tax = await Taxonomy.load(session)
    errors = []
    for fld, kind in (("equipment_categories", "equipment"), ("service_categories", "services"),
                      ("industries", "industries"), ("certifications", "certifications")):  # fmt: skip
        for i, code in enumerate(getattr(body, fld)):
            if (kind, code) not in tax.ids:
                errors.append({"field": f"{fld}.{i}", "message": f"unknown {kind} code {code!r}"})
    if errors:
        raise ValidationFailed("Unknown taxonomy codes", errors=errors)


@router.post("/discovery/estimate", response_model=DiscoveryEstimate)
async def estimate_discovery(body: DiscoveryRequest, _: Discoverer, c: ContainerDep) -> DiscoveryEstimate:
    """Expected number of searches and cost of a discovery, before running it."""
    return estimate(body, c.settings)


@router.post("/discovery/vendors", response_model=DiscoveryAccepted, status_code=status.HTTP_202_ACCEPTED)
async def start_vendor_discovery(
    body: DiscoveryRequest,
    principal: Discoverer,
    session: SessionDep,
    c: ContainerDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> DiscoveryAccepted:
    """Finds suppliers matching the request on the web, researches their sites and proposes what it finds for
    curator review. Runs as a background job; follow it with GET /jobs/{id}/events."""
    if not c.ai.configured:
        raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
    await _validate_codes(session, body)
    est = estimate(body, c.settings)
    # Starting needs room for a typical run; each AI call re-checks, so a run stops before overspending much.
    await c.ai.ensure_budget(CallContext(principal.tenant_id), Decimal(str(est.typical_cost_usd)))
    job = await JobService(session, principal, c.job_runner.notify).create(
        JobType.VENDOR_DISCOVERY, {"request": body.model_dump(mode="json"), "estimate": est.model_dump()},
        idempotency_key,
    )  # fmt: skip
    prefix = c.settings.api_prefix
    return DiscoveryAccepted(job_id=job.id, status=job.status, events_url=f"{prefix}/jobs/{job.id}/events",
                             estimate=est)  # fmt: skip


@router.get("/discovery/runs", response_model=CursorPage[JobRead])
async def discovery_runs(
    _: Discoverer,
    principal: PrincipalDep,
    session: SessionDep,
    c: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> CursorPage[JobRead]:
    svc = JobService(session, principal, c.job_runner.notify)
    rows, nxt = await svc.list_page(
        job_type=JobType.VENDOR_DISCOVERY, status=None, limit=limit, cursor=cursor
    )
    return CursorPage(
        items=[JobRead.model_validate(j) for j in rows], next_cursor=nxt, has_more=nxt is not None
    )


@router.get("/discovery/runs/{job_id}/results", response_model=ImportRowPage)
async def discovery_results(
    job_id: uuid.UUID,
    _: Discoverer,
    principal: PrincipalDep,
    session: SessionDep,
    c: ContainerDep,
    outcome: ImportRowOutcome | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> ImportRowPage:
    """One entry per candidate: new (proposed), existing (matched, new facts proposed), or discarded with the reason."""
    svc = ImportService(session, principal, c.settings)
    rows, nxt, counts = await svc.rows(job_id, outcome=outcome, limit=limit, cursor=cursor)
    return ImportRowPage(items=[ImportRowRead.model_validate(r) for r in rows], next_cursor=nxt,
                         has_more=nxt is not None, counts=counts)  # fmt: skip


# ---------------------------------------------------------------- one known vendor

CAPABILITY_JOBS: dict[str, tuple[JobType, list[str]]] = {
    "equipment": (JobType.EQUIPMENT_DISCOVERY, ["equipment", "certifications"]),
    "services": (JobType.SERVICE_DISCOVERY, ["services"]),
    "refresh": (JobType.ENRICHMENT, list(ALL_SCOPES)),
}


async def _start_capability_job(
    kind: str, vendor_id: uuid.UUID, body: CapabilityRequest, principal: Principal, session: SessionDep,
    c: ContainerDep, idempotency_key: str | None,
) -> DiscoveryAccepted:  # fmt: skip
    if not c.ai.configured:
        raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
    vendor = await session.get(Vendor, vendor_id)
    if vendor is None or vendor.status == VendorStatus.RETIRED:
        raise NotFound("Vendor not found")
    if not (vendor.website or vendor.domains or body.website):
        raise ValidationFailed("This vendor has no website to research; give one (website) or add it first")
    job_type, scope = CAPABILITY_JOBS[kind]
    est = capability_estimate(body.max_pages, c.settings)
    await c.ai.ensure_budget(CallContext(principal.tenant_id), Decimal(str(est.typical_cost_usd)))
    job = await JobService(session, principal, c.job_runner.notify).create(
        job_type, {"vendor_id": str(vendor_id), "vendor_name": vendor.display_name, "scope": scope,
                   "request": body.model_dump(mode="json"), "estimate": est.model_dump()},
        idempotency_key,
    )  # fmt: skip
    return DiscoveryAccepted(job_id=job.id, status=job.status,
                             events_url=f"{c.settings.api_prefix}/jobs/{job.id}/events", estimate=est)  # fmt: skip


def _capability_route(kind: str, summary: str) -> None:
    async def start(
        vendor_id: uuid.UUID,
        principal: Discoverer,
        session: SessionDep,
        c: ContainerDep,
        body: CapabilityRequest | None = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
    ) -> DiscoveryAccepted:
        return await _start_capability_job(kind, vendor_id, body or CapabilityRequest(), principal, session, c,
                                           idempotency_key)  # fmt: skip

    start.__name__ = f"discover_vendor_{kind}"
    router.add_api_route(f"/discovery/vendors/{{vendor_id}}/{kind}", start, methods=["POST"],
                         response_model=DiscoveryAccepted, status_code=status.HTTP_202_ACCEPTED,
                         summary=summary)  # fmt: skip


_capability_route("equipment", "Research a vendor's website for equipment and certifications")
_capability_route("services", "Research a vendor's website for services")
_capability_route("refresh", "Re-research everything about a vendor (all fact types)")


# ---------------------------------------------------------------- duplicates (curators)


class DedupScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_similarity: float = Field(default=0.6, ge=0.5, le=0.99)
    max_pairs: int = Field(default=200, ge=1, le=1000)
    use_ai: bool = True  # let the AI judge pairs whose names are similar but not near-identical


@router.post("/admin/dedup/scan", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def start_dedup_scan(
    body: DedupScanRequest,
    principal: Annotated[Principal, Depends(require(P.KNOWLEDGE_APPROVE))],
    session: SessionDep,
    c: ContainerDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> JobAccepted:
    """Scans the knowledge base for duplicate vendors and raises merge proposals (nothing is merged without review)."""
    job = await JobService(session, principal, c.job_runner.notify).create(
        JobType.DEDUP_SCAN, body.model_dump(), idempotency_key
    )
    return JobAccepted(
        job_id=job.id, status=job.status, events_url=f"{c.settings.api_prefix}/jobs/{job.id}/events"
    )
