import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.ai.service import AINotAvailable
from app.api.deps import ContainerDep, SessionDep, require
from app.domain.enums import JobType, VendorStatus
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.jobs.schemas import JobAccepted
from app.modules.jobs.service import JobService
from app.modules.search.indexing import stale_vendors_query
from app.modules.search.models import VendorEmbedding
from app.modules.search.schemas import (
    ParseRequest,
    ParseResult,
    SearchHistoryRead,
    SearchRequest,
    SearchResponse,
    Suggestion,
)
from app.modules.search.service import SearchService
from app.modules.vendors.models import Vendor

router = APIRouter(tags=["search"])

Curator = Annotated[Principal, Depends(require(P.KNOWLEDGE_APPROVE))]


class IndexStatus(BaseModel):
    vendors: int
    indexed_vendors: int
    stale_vendors: int
    chunks: int


class ReindexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)  # empty: every stale vendor


@router.get("/admin/search/index", response_model=IndexStatus)
async def index_status(_: Curator, session: SessionDep) -> IndexStatus:
    """How much of the knowledge base has current search vectors."""
    vendors = await session.scalar(select(func.count()).where(Vendor.status != VendorStatus.RETIRED)) or 0
    stale = (
        await session.scalar(select(func.count()).select_from(stale_vendors_query(10_000_000).subquery()))
        or 0
    )
    indexed = await session.scalar(select(func.count(func.distinct(VendorEmbedding.vendor_id)))) or 0
    chunks = await session.scalar(select(func.count()).select_from(VendorEmbedding)) or 0
    return IndexStatus(vendors=vendors, indexed_vendors=indexed, stale_vendors=stale, chunks=chunks)


@router.post("/admin/search/reindex", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def reindex(
    body: ReindexRequest,
    principal: Curator,
    session: SessionDep,
    c: ContainerDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> JobAccepted:
    """Refreshes search vectors now (normally automatic after approvals and in the maintenance loop)."""
    if not c.ai.configured:
        raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
    payload = {"vendor_ids": [str(v) for v in body.vendor_ids]} if body.vendor_ids else {}
    job = await JobService(session, principal, c.job_runner.notify).create(
        JobType.EMBEDDING, payload, idempotency_key
    )
    return JobAccepted(
        job_id=job.id, status=job.status, events_url=f"{c.settings.api_prefix}/jobs/{job.id}/events"
    )


# ---------------------------------------------------------------- search

Searcher = Annotated[Principal, Depends(require(P.SEARCH_RUN))]


def _svc(session: SessionDep, principal: Searcher, c: ContainerDep) -> SearchService:
    return SearchService(session, principal, c.ai)


Svc = Annotated[SearchService, Depends(_svc)]


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, svc: Svc) -> SearchResponse:
    """Hybrid vendor search: the query (or edited intent chips) ranked over structured facts, full text and
    vector similarity, with a confidence score and the facts behind each match."""
    return await svc.search(body)


@router.post("/search/parse", response_model=ParseResult)
async def parse(body: ParseRequest, svc: Svc) -> ParseResult:
    """Only the query understanding step, for the chips UI ("ai" costs a fraction of a cent; repeat queries are free)."""
    return await svc.parse(body.query, body.parser)


@router.get("/search/history", response_model=list[SearchHistoryRead])
async def history(svc: Svc, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[SearchHistoryRead]:
    return [SearchHistoryRead.model_validate(h) for h in await svc.history(limit)]


@router.delete("/search/history/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_history(entry_id: uuid.UUID, svc: Svc) -> None:
    await svc.delete_history(entry_id)


@router.get("/search/suggest", response_model=list[Suggestion])
async def suggest(svc: Svc, q: Annotated[str, Query(max_length=100)] = "") -> list[Suggestion]:
    """Typeahead: matching categories, certifications, industries and vendor names."""
    return await svc.suggest(q)
