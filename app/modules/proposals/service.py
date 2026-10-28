"""Proposal lifecycle: submit, review (edit / approve / reject / request info) and apply.

`submit` is also the entry point for automated sources (Excel/PDF import, AI discovery). It does not commit, so
an import can create many proposals in one transaction; HTTP handlers commit after calling it.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, NotFound, ValidationFailed, VersionConflict
from app.core.pagination import paginate
from app.domain.enums import (
    ActorType,
    ApprovalAction,
    ProposalItemAction,
    ProposalItemStatus,
    ProposalKind,
    ProposalOrigin,
    ProposalStatus,
    VendorStatus,
)
from app.domain.principal import Principal
from app.modules.audit.service import record_audit
from app.modules.proposals.applier import Applier
from app.modules.proposals.models import ApprovalHistory, KnowledgeProposal, ProposalItem
from app.modules.proposals.payloads import validate_evidence, validate_payload
from app.modules.proposals.schemas import ItemCounts, ProposalSummary
from app.modules.vendors.models import Vendor


@dataclass
class NewItem:
    action: ProposalItemAction
    payload: dict[str, Any]
    evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence: Decimal | None = None


def summarize(p: KnowledgeProposal) -> ProposalSummary:
    counts = ItemCounts()
    for item in p.items:
        setattr(counts, item.status.value, getattr(counts, item.status.value) + 1)
    return ProposalSummary(
        id=p.id,
        kind=p.kind,
        origin=p.origin,
        status=p.status,
        title=p.title,
        subject_vendor_id=p.subject_vendor_id,
        confidence=p.confidence,
        counts=counts,
        created_at=p.created_at,
        decided_at=p.decided_at,
        version=p.version,
    )


class ProposalService:
    def __init__(self, session: AsyncSession, principal: Principal | None) -> None:
        self.s = session
        self.principal = principal  # None = system (automated import/auto-approval)

    @property
    def _actor_id(self) -> uuid.UUID | None:
        return self.principal.user_id if self.principal else None

    def _audit(self, action: str, proposal: KnowledgeProposal, **changes: Any) -> None:
        record_audit(
            self.s,
            action,
            principal=self.principal,
            actor_type=None if self.principal else ActorType.SYSTEM,
            tenant_id=None,  # the knowledge base is global: these are platform-level events
            entity_type="proposal",
            entity_id=proposal.id,
            changes=changes,
        )

    # ------------------------------------------------------------------ submit

    async def submit(
        self,
        kind: ProposalKind,
        origin: ProposalOrigin,
        items: list[NewItem],
        *,
        title: str | None = None,
        subject_vendor_id: uuid.UUID | None = None,
        origin_tenant_id: uuid.UUID | None = None,
        origin_user_id: uuid.UUID | None = None,
        origin_job_id: uuid.UUID | None = None,
        confidence: Decimal | None = None,
        auto_approve: bool = False,
    ) -> KnowledgeProposal:
        if not items:
            raise ValidationFailed("A proposal needs at least one item")
        creates = [i for i in items if i.action == ProposalItemAction.CREATE_VENDOR]
        if kind == ProposalKind.NEW_VENDOR:
            if len(creates) != 1 or items[0].action != ProposalItemAction.CREATE_VENDOR:
                raise ValidationFailed("A new-vendor proposal starts with exactly one create_vendor item")
            if subject_vendor_id is not None:
                raise ValidationFailed("A new-vendor proposal can't have a subject vendor")
        else:
            if creates:
                raise ValidationFailed("create_vendor is only allowed in a new-vendor proposal")
            if subject_vendor_id is None:
                raise ValidationFailed("An update proposal needs subject_vendor_id")
            vendor = await self.s.get(Vendor, subject_vendor_id)
            if vendor is None or vendor.status == VendorStatus.RETIRED:
                raise NotFound("Vendor not found")
            title = title or vendor.display_name

        rows: list[ProposalItem] = []
        for idx, item in enumerate(items):
            payload = validate_payload(item.action, item.payload, field=f"items.{idx}.payload")
            evidence = validate_evidence(item.evidence, field=f"items.{idx}.evidence")
            rows.append(
                ProposalItem(
                    sort_order=idx,
                    action=item.action,
                    payload=payload,
                    evidence=evidence,
                    confidence=item.confidence,
                    status=ProposalItemStatus.PENDING,
                )
            )
        if title is None:
            title = rows[0].payload.get("legal_name", "Proposal")

        proposal = KnowledgeProposal(
            kind=kind,
            origin=origin,
            status=ProposalStatus.PENDING_REVIEW,
            title=title[:300],
            subject_vendor_id=subject_vendor_id,
            origin_tenant_id=origin_tenant_id,
            origin_user_id=origin_user_id,
            origin_job_id=origin_job_id,
            confidence=confidence,
            decision_note="",
            version=1,
            items=rows,
        )
        self.s.add(proposal)
        await self.s.flush()
        self._audit("proposal.submitted", proposal, kind=kind, origin=origin, items=len(rows))
        if auto_approve:
            await self._decide(proposal, approve=[r.id for r in rows], reject=[], note="", auto=True)
        return proposal

    # ------------------------------------------------------------------ read

    def _query(self) -> Select[KnowledgeProposal]:
        return select(KnowledgeProposal)

    async def list_page(
        self,
        *,
        status: ProposalStatus | None,
        kind: ProposalKind | None,
        origin: ProposalOrigin | None,
        origin_tenant_id: uuid.UUID | None = None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[KnowledgeProposal], str | None]:
        stmt = self._query()
        if status:
            stmt = stmt.where(KnowledgeProposal.status == status)
        if kind:
            stmt = stmt.where(KnowledgeProposal.kind == kind)
        if origin:
            stmt = stmt.where(KnowledgeProposal.origin == origin)
        if origin_tenant_id:
            stmt = stmt.where(KnowledgeProposal.origin_tenant_id == origin_tenant_id)
        rows, nxt = await paginate(
            self.s,
            stmt,
            created_at_col=KnowledgeProposal.created_at,
            id_col=KnowledgeProposal.id,
            limit=limit,
            cursor=cursor,
        )
        return list(rows), nxt

    async def get(self, proposal_id: uuid.UUID, *, lock: bool = False) -> KnowledgeProposal:
        stmt = select(KnowledgeProposal).where(KnowledgeProposal.id == proposal_id)
        if lock:
            stmt = stmt.with_for_update()
        proposal = await self.s.scalar(stmt)
        if proposal is None:
            raise NotFound("Proposal not found")
        return proposal

    async def history(self, proposal_id: uuid.UUID) -> list[ApprovalHistory]:
        return list(
            await self.s.scalars(
                select(ApprovalHistory)
                .where(ApprovalHistory.proposal_id == proposal_id)
                .order_by(ApprovalHistory.created_at, ApprovalHistory.id)
            )
        )

    # ------------------------------------------------------------------ review

    @staticmethod
    def _check_open(proposal: KnowledgeProposal, version: int) -> None:
        if proposal.version != version:
            raise VersionConflict("This proposal changed since you loaded it; reload and review again")
        if not proposal.status.is_open:
            raise Conflict(f"This proposal is already {proposal.status.value}", code="proposal_closed")

    async def edit_item(
        self,
        proposal_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        payload: dict[str, Any] | None,
        evidence: list[dict[str, Any]] | None,
        version: int,
    ) -> KnowledgeProposal:
        proposal = await self.get(proposal_id, lock=True)
        self._check_open(proposal, version)
        item = next((i for i in proposal.items if i.id == item_id), None)
        if item is None:
            raise NotFound("Item not found")
        if item.status != ProposalItemStatus.PENDING:
            raise Conflict("Only pending items can be edited", code="item_decided")
        before = {"payload": item.payload, "evidence": item.evidence}
        if payload is not None:
            item.payload = validate_payload(item.action, payload)
        if evidence is not None:
            item.evidence = validate_evidence(evidence)
        if item.action == ProposalItemAction.CREATE_VENDOR:
            proposal.title = item.payload.get("legal_name", proposal.title)[:300]
        proposal.version += 1
        self.s.add(
            ApprovalHistory(
                proposal_id=proposal.id,
                proposal_item_id=item.id,
                action=ApprovalAction.EDIT,
                before=before,
                after={"payload": item.payload, "evidence": item.evidence},
                actor_id=self._actor_id,
            )
        )
        self._audit("proposal.item_edited", proposal, item_id=str(item.id))
        await self.s.commit()
        return proposal

    async def approve(
        self, proposal_id: uuid.UUID, item_ids: list[uuid.UUID] | None, note: str, version: int
    ) -> KnowledgeProposal:
        proposal = await self.get(proposal_id, lock=True)
        self._check_open(proposal, version)
        approve = self._select_pending(proposal, item_ids)
        await self._decide(proposal, approve=approve, reject=[], note=note, auto=False)
        await self.s.commit()
        return proposal

    async def reject(
        self, proposal_id: uuid.UUID, item_ids: list[uuid.UUID] | None, note: str, version: int
    ) -> KnowledgeProposal:
        if not note.strip():
            raise ValidationFailed(
                "Give a reason", errors=[{"field": "note", "message": "Required when rejecting"}]
            )
        proposal = await self.get(proposal_id, lock=True)
        self._check_open(proposal, version)
        reject = self._select_pending(proposal, item_ids)
        await self._decide(proposal, approve=[], reject=reject, note=note, auto=False)
        await self.s.commit()
        return proposal

    async def request_info(self, proposal_id: uuid.UUID, note: str, version: int) -> KnowledgeProposal:
        proposal = await self.get(proposal_id, lock=True)
        self._check_open(proposal, version)
        proposal.status = ProposalStatus.NEEDS_INFO
        proposal.decision_note = note
        proposal.version += 1
        self.s.add(
            ApprovalHistory(
                proposal_id=proposal.id,
                action=ApprovalAction.REQUEST_INFO,
                reason=note,
                actor_id=self._actor_id,
            )
        )
        self._audit("proposal.info_requested", proposal, note=note)
        await self.s.commit()
        return proposal

    @staticmethod
    def _select_pending(proposal: KnowledgeProposal, item_ids: list[uuid.UUID] | None) -> list[uuid.UUID]:
        pending = {i.id for i in proposal.items if i.status == ProposalItemStatus.PENDING}
        if item_ids is None:
            chosen = pending
        else:
            unknown = set(item_ids) - {i.id for i in proposal.items}
            if unknown:
                raise NotFound(f"Unknown item(s): {', '.join(sorted(map(str, unknown)))}")
            already = set(item_ids) - pending
            if already:
                raise Conflict("Some items were already decided", code="item_decided")
            chosen = set(item_ids)
        if not chosen:
            raise Conflict("Nothing left to decide", code="nothing_pending")
        return [i.id for i in proposal.items if i.id in chosen]  # keep item order

    async def _decide(
        self,
        proposal: KnowledgeProposal,
        *,
        approve: list[uuid.UUID],
        reject: list[uuid.UUID],
        note: str,
        auto: bool,
    ) -> None:
        now = datetime.now(UTC)
        by_id = {i.id: i for i in proposal.items}
        approve_set, reject_set = set(approve), set(reject)
        create = next((i for i in proposal.items if i.action == ProposalItemAction.CREATE_VENDOR), None)

        if create is not None and create.status == ProposalItemStatus.PENDING:
            if create.id in reject_set:
                # Without the vendor nothing else can be applied: reject the rest with it.
                reject_set |= {i.id for i in proposal.items if i.status == ProposalItemStatus.PENDING}
                approve_set.clear()
            elif approve_set and create.id not in approve_set:
                raise Conflict(
                    "Approve the vendor itself (create_vendor) before its facts", code="vendor_first"
                )

        applier = Applier(self.s)
        vendor_id = proposal.subject_vendor_id
        for item_id in [i.id for i in proposal.items if i.id in approve_set]:  # create_vendor is always first
            item = by_id[item_id]
            result = await applier.apply(item, vendor_id)
            if item.action == ProposalItemAction.CREATE_VENDOR:
                vendor_id = result.vendor_id
                proposal.subject_vendor_id = vendor_id
            item.status = ProposalItemStatus.APPLIED
            item.applied_table, item.applied_entity_id = result.table, result.entity_id
            item.decided_by, item.decided_at = self._actor_id, now
            self.s.add(
                ApprovalHistory(
                    proposal_id=proposal.id,
                    proposal_item_id=item.id,
                    action=ApprovalAction.AUTO_APPROVE if auto else ApprovalAction.APPROVE,
                    reason=note,
                    after={"table": result.table, "id": str(result.entity_id)},
                    actor_id=self._actor_id,
                )
            )
        for item_id in reject_set:
            item = by_id[item_id]
            item.status = ProposalItemStatus.REJECTED
            item.reject_reason = note
            item.decided_by, item.decided_at = self._actor_id, now
            self.s.add(
                ApprovalHistory(
                    proposal_id=proposal.id,
                    proposal_item_id=item.id,
                    action=ApprovalAction.REJECT,
                    reason=note,
                    actor_id=self._actor_id,
                )
            )

        statuses = {i.status for i in proposal.items}
        if ProposalItemStatus.PENDING in statuses:
            proposal.status = ProposalStatus.PENDING_REVIEW
        else:
            if statuses == {ProposalItemStatus.APPLIED}:
                proposal.status = ProposalStatus.AUTO_APPROVED if auto else ProposalStatus.APPROVED
            elif statuses == {ProposalItemStatus.REJECTED}:
                proposal.status = ProposalStatus.REJECTED
            else:
                proposal.status = ProposalStatus.PARTIALLY_APPROVED
            proposal.decided_by, proposal.decided_at = self._actor_id, now
        if note:
            proposal.decision_note = note
        proposal.version += 1
        await self.s.flush()
        self._audit(
            "proposal.auto_approved" if auto else "proposal.decided",
            proposal,
            approved=len(approve_set),
            rejected=len(reject_set),
            status=proposal.status,
        )
