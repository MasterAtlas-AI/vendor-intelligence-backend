import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, require
from app.core.exceptions import AppError, Forbidden, ValidationFailed
from app.db.session import open_session
from app.domain.enums import ProposalKind, ProposalOrigin, ProposalStatus
from app.domain.permissions import P
from app.domain.principal import Principal, TenantContext
from app.modules.imports.linking import link_approved_imports
from app.modules.proposals.models import KnowledgeProposal
from app.modules.proposals.schemas import (
    BulkDecision,
    BulkOutcome,
    BulkResult,
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
from app.modules.vendors.models import Vendor
from app.modules.vendors.service import VendorQueryService
from app.modules.webhooks.service import emit, emit_vendor_updated

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
    await _after_approval(c, [Approved.of(proposal)])
    return await _detail(svc, principal, proposal)


@dataclass(frozen=True)
class Approved:
    """What the follow-up steps need from an approved proposal, read while it is loaded (a later rollback in the same
    session would expire the ORM object)."""

    id: uuid.UUID
    kind: ProposalKind
    status: ProposalStatus
    title: str
    origin_tenant_id: uuid.UUID | None
    subject_vendor_id: uuid.UUID | None

    @classmethod
    def of(cls, p: KnowledgeProposal) -> "Approved":
        return cls(p.id, p.kind, p.status, p.title, p.origin_tenant_id, p.subject_vendor_id)


async def _after_approval(c: ContainerDep, proposals: list[Approved]) -> None:
    """What follows approvals, once per batch (platform mode, after the commits)."""
    if not proposals:
        return
    # A newly created vendor joins the lists of tenants who imported it.
    await link_approved_imports(c.sessionmaker, [p.id for p in proposals])
    if any(p.kind == ProposalKind.MERGE_VENDORS for p in proposals):
        await finish_merges(c.sessionmaker)  # organizations' own records follow the merge
    async with open_session(c.sessionmaker, TenantContext.platform()) as s:
        if c.ai.configured:  # refresh search vectors for what just changed
            await queue_refresh_if_stale(s)
        # Webhooks: the submitting organization hears its proposal was decided; organizations with the vendor on
        # their list hear it changed.
        for proposal in proposals:
            if proposal.origin_tenant_id:
                await emit(
                    s,
                    proposal.origin_tenant_id,
                    "proposal.approved",
                    {
                        "proposal_id": str(proposal.id),
                        "kind": proposal.kind.value,
                        "status": proposal.status.value,
                        "title": proposal.title,
                        "vendor_id": str(proposal.subject_vendor_id or ""),
                    },
                )
            if proposal.subject_vendor_id and (vendor := await s.get(Vendor, proposal.subject_vendor_id)):
                await emit_vendor_updated(s, vendor.id, vendor.display_name, vendor.knowledge_version)
        await s.commit()
    c.job_runner.notify()


@router.post("/admin/proposals/bulk", response_model=BulkResult)
async def decide_in_bulk(body: BulkDecision, principal: Curator, svc: Svc, c: ContainerDep) -> BulkResult:
    """Approve or reject several proposals (every pending item of each). Each one succeeds or fails on its own: a
    proposal that changed since it was listed, or whose facts clash (e.g. a website another vendor owns), is
    reported and left as it was."""
    if body.action == "reject" and not body.note.strip():
        raise ValidationFailed(
            "Give a reason", errors=[{"field": "note", "message": "Required when rejecting"}]
        )
    results: list[BulkOutcome] = []
    approved: list[Approved] = []
    for target in body.proposals:
        try:
            if body.action == "approve":
                proposal = await svc.approve(target.id, None, body.note, target.version)
                approved.append(Approved.of(proposal))
            else:
                proposal = await svc.reject(target.id, None, body.note, target.version)
            results.append(BulkOutcome(id=target.id, ok=True, status=proposal.status))
            svc.s.expunge_all()  # done with it; keeps a later rollback from touching it
        except AppError as exc:
            await svc.s.rollback()  # this proposal stays as it was; carry on with the next
            results.append(BulkOutcome(id=target.id, ok=False, error=exc.detail or exc.title))
    await _after_approval(c, approved)
    done = sum(1 for r in results if r.ok)
    return BulkResult(done=done, failed=len(results) - done, results=results)


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
