import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, require
from app.core.exceptions import Forbidden
from app.db.session import open_session
from app.domain.enums import ProposalKind, ProposalOrigin, ProposalStatus
from app.domain.permissions import P
from app.domain.principal import Principal, TenantContext
from app.modules.imports.linking import link_approved_imports
from app.modules.proposals.models import KnowledgeProposal
from app.modules.proposals.schemas import (
    Decision,
    HistoryRead,
    ItemRead,
    ItemUpdate,
    ProposalCreate,
    ProposalDetail,
    ProposalPage,
    RequestInfo,
)
from app.modules.proposals.service import NewItem, ProposalService, summarize
from app.modules.search.indexing import queue_refresh_if_stale
from app.modules.vendors.merge import finish_merges
from app.modules.vendors.service import VendorQueryService

router = APIRouter(tags=["proposals"])
Curator = Annotated[Principal, Depends(require(P.KNOWLEDGE_APPROVE))]


def get_service(session: SessionDep, principal: PrincipalDep) -> ProposalService:
    return ProposalService(session, principal)


Svc = Annotated[ProposalService, Depends(get_service)]


async def _detail(svc: ProposalService, principal: Principal, proposal: KnowledgeProposal) -> ProposalDetail:
    subject = (
        await VendorQueryService(svc.s, principal).detail(proposal.subject_vendor_id)
        if proposal.subject_vendor_id
        else None
    )
    return ProposalDetail(
        **summarize(proposal).model_dump(),
        origin_tenant_id=proposal.origin_tenant_id,
        origin_job_id=proposal.origin_job_id,
        decision_note=proposal.decision_note,
        items=[ItemRead.model_validate(i) for i in proposal.items],
        history=[HistoryRead.model_validate(h) for h in await svc.history(proposal.id)],
        subject_vendor=subject,
    )


# ---------------------------------------------------------------- curators


@router.get("/admin/proposals", response_model=ProposalPage)
async def list_proposals(
    _: Curator,
    svc: Svc,
    status_: Annotated[ProposalStatus | None, Query(alias="status")] = None,
    kind: ProposalKind | None = None,
    origin: ProposalOrigin | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> ProposalPage:
    rows, nxt = await svc.list_page(status=status_, kind=kind, origin=origin, limit=limit, cursor=cursor)
    return ProposalPage(items=[summarize(p) for p in rows], next_cursor=nxt, has_more=nxt is not None)


@router.post("/admin/proposals", response_model=ProposalDetail, status_code=status.HTTP_201_CREATED)
async def create_proposal(body: ProposalCreate, principal: Curator, svc: Svc) -> ProposalDetail:
    """Enter knowledge by hand. It still goes through review, so a second curator can check it."""
    proposal = await svc.submit(
        body.kind,
        ProposalOrigin.MANUAL,
        [NewItem(i.action, i.payload, i.evidence, i.confidence) for i in body.items],
        title=body.title,
        subject_vendor_id=body.subject_vendor_id,
        origin_user_id=principal.user_id,
    )
    await svc.s.commit()
    return await _detail(svc, principal, proposal)


@router.get("/admin/proposals/{proposal_id}", response_model=ProposalDetail)
async def get_proposal(proposal_id: uuid.UUID, principal: Curator, svc: Svc) -> ProposalDetail:
    return await _detail(svc, principal, await svc.get(proposal_id))


@router.patch("/admin/proposals/{proposal_id}/items/{item_id}", response_model=ProposalDetail)
async def edit_item(
    proposal_id: uuid.UUID, item_id: uuid.UUID, body: ItemUpdate, principal: Curator, svc: Svc
) -> ProposalDetail:
    proposal = await svc.edit_item(
        proposal_id, item_id, payload=body.payload, evidence=body.evidence, version=body.version
    )
    return await _detail(svc, principal, proposal)


@router.post("/admin/proposals/{proposal_id}/approve", response_model=ProposalDetail)
async def approve(
    proposal_id: uuid.UUID, body: Decision, principal: Curator, svc: Svc, c: ContainerDep
) -> ProposalDetail:
    proposal = await svc.approve(proposal_id, body.item_ids, body.note, body.version)
    # A newly created vendor joins the lists of tenants who imported it (platform-mode, after the commit).
    await link_approved_imports(c.sessionmaker, [proposal.id])
    if proposal.kind == ProposalKind.MERGE_VENDORS:
        await finish_merges(c.sessionmaker)  # organizations' own records follow the merge (platform mode)
    if c.ai.configured:  # refresh search vectors for what just changed
        async with open_session(c.sessionmaker, TenantContext.platform()) as s:
            if await queue_refresh_if_stale(s):
                await s.commit()
                c.job_runner.notify()
    return await _detail(svc, principal, proposal)


@router.post("/admin/proposals/{proposal_id}/reject", response_model=ProposalDetail)
async def reject(proposal_id: uuid.UUID, body: Decision, principal: Curator, svc: Svc) -> ProposalDetail:
    proposal = await svc.reject(proposal_id, body.item_ids, body.note, body.version)
    return await _detail(svc, principal, proposal)


@router.post("/admin/proposals/{proposal_id}/request-info", response_model=ProposalDetail)
async def request_info(
    proposal_id: uuid.UUID, body: RequestInfo, principal: Curator, svc: Svc
) -> ProposalDetail:
    proposal = await svc.request_info(proposal_id, body.note, body.version)
    return await _detail(svc, principal, proposal)


# ---------------------------------------------------------------- tenants


@router.get("/proposals/mine", response_model=ProposalPage)
async def my_proposals(
    principal: Annotated[Principal, Depends(require(P.VENDOR_READ))],
    svc: Svc,
    status_: Annotated[ProposalStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> ProposalPage:
    """Proposals that came from your organization (imports, suggestions), with their review status."""
    if principal.tenant_id is None:
        raise Forbidden("No tenant selected", code="tenant_required")
    rows, nxt = await svc.list_page(
        status=status_,
        kind=None,
        origin=None,
        origin_tenant_id=principal.tenant_id,
        limit=limit,
        cursor=cursor,
    )
    return ProposalPage(items=[summarize(p) for p in rows], next_cursor=nxt, has_more=nxt is not None)
