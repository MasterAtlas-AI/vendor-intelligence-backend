"""RFQs: status workflow, vendors and quotes, comparison with scorecards.

Workflow: draft -> issued (needs at least one vendor; shortlisted vendors become invited) -> evaluating -> awarded
(to a vendor that quoted; the other quoted vendors become lost) -> closed. Anything not closed can be cancelled.
`rfq:manage` edits (tenant admins, managers, engineers); everyone in the organization can read. Every status change
is audited.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, Forbidden, NotFound, ValidationFailed, VersionConflict
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.audit.service import record_audit
from app.modules.projects.models import Project, ProjectVendor, Scorecard
from app.modules.rfqs.models import Rfq, RfqVendor
from app.modules.rfqs.schemas import (
    AwardRequest,
    RfqCreate,
    RfqRead,
    RfqUpdate,
    RfqVendorRead,
    RfqVendorUpdate,
    Transition,
)
from app.modules.taxonomy.models import EquipmentCategory
from app.modules.vendors.models import Vendor
from app.modules.vendors.service import VendorFilters, VendorQueryService

# status -> statuses it may move to by a plain transition (award has its own endpoint)
TRANSITIONS = {
    "draft": {"issued", "cancelled"},
    "issued": {"evaluating", "cancelled"},
    "evaluating": {"cancelled"},
    "awarded": {"closed"},
}


class RfqService:
    def __init__(self, session: AsyncSession, principal: Principal) -> None:
        if principal.tenant_id is None:
            raise Forbidden("RFQs belong to an organization; open the app as one to use them")
        self.s = session
        self.principal = principal
        self.tenant_id: uuid.UUID = principal.tenant_id

    @property
    def can_edit(self) -> bool:
        return self.principal.has(P.RFQ_MANAGE)

    def _require_edit(self) -> None:
        if not self.can_edit:
            raise Forbidden("Changing RFQs needs the rfq:manage permission")

    async def _rfq(self, rfq_id: uuid.UUID) -> Rfq:
        rfq = await self.s.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound("RFQ not found")
        return rfq

    async def _read(self, rfq: Rfq) -> RfqRead:
        out = RfqRead.model_validate(rfq)
        if rfq.project_id:
            out.project_name = await self.s.scalar(select(Project.name).where(Project.id == rfq.project_id))
        if rfq.awarded_vendor_id:
            out.awarded_vendor_name = await self.s.scalar(
                select(Vendor.display_name).where(Vendor.id == rfq.awarded_vendor_id)
            )
        counts = await self.s.execute(
            select(RfqVendor.status, func.count())
            .where(RfqVendor.rfq_id == rfq.id)
            .group_by(RfqVendor.status)
        )
        out.vendor_counts = {k: int(v) for k, v in counts.all()}
        out.can_edit = self.can_edit
        return out

    async def _next_number(self) -> str:
        year = datetime.now(UTC).year
        n = await self.s.scalar(select(func.count()).where(Rfq.rfq_number.like(f"RFQ-{year}-%"))) or 0
        return f"RFQ-{year}-{n + 1:03d}"

    async def _check_codes(self, codes: list[str]) -> list[str]:
        if not codes:
            return []
        known = set(
            await self.s.scalars(select(EquipmentCategory.code).where(EquipmentCategory.code.in_(codes)))
        )
        if unknown := [c for c in codes if c not in known]:
            raise ValidationFailed(f"Unknown equipment categories: {', '.join(unknown)}")
        return list(dict.fromkeys(codes))

    # ------------------------------------------------------------------ RFQs

    async def list_rfqs(self, *, status: str | None, project_id: uuid.UUID | None) -> list[RfqRead]:
        stmt = select(Rfq)
        if status:
            stmt = stmt.where(Rfq.status == status)
        if project_id:
            stmt = stmt.where(Rfq.project_id == project_id)
        return [
            await self._read(r) for r in await self.s.scalars(stmt.order_by(Rfq.updated_at.desc()).limit(200))
        ]

    async def get(self, rfq_id: uuid.UUID) -> RfqRead:
        return await self._read(await self._rfq(rfq_id))

    async def create(self, body: RfqCreate) -> RfqRead:
        self._require_edit()
        if body.project_id and await self.s.get(Project, body.project_id) is None:
            raise NotFound("Project not found")
        if body.from_shortlist and not body.project_id:
            raise ValidationFailed("from_shortlist needs a project_id")
        rfq = Rfq(
            tenant_id=self.tenant_id,
            project_id=body.project_id,
            title=body.title,
            rfq_number=body.rfq_number or await self._next_number(),
            description=body.description,
            equipment_categories=await self._check_codes(body.equipment_categories),
            requirements=[r.model_dump() for r in body.requirements],
            due_date=body.due_date,
        )
        self.s.add(rfq)
        try:
            await self.s.flush()
        except IntegrityError as exc:
            raise Conflict(f"RFQ number {rfq.rfq_number!r} is already used") from exc
        if body.from_shortlist:
            vendor_ids = await self.s.scalars(
                select(ProjectVendor.vendor_id).where(
                    ProjectVendor.project_id == body.project_id,
                    ProjectVendor.stage.in_(("shortlist", "selected")),
                )
            )
            for vid in vendor_ids:
                self.s.add(RfqVendor(tenant_id=self.tenant_id, rfq_id=rfq.id, vendor_id=vid))
        record_audit(
            self.s,
            "rfq.created",
            principal=self.principal,
            entity_type="rfq",
            entity_id=rfq.id,
            changes={"rfq_number": rfq.rfq_number, "title": rfq.title},
        )
        await self.s.commit()
        await self.s.refresh(rfq)
        return await self._read(rfq)

    async def update(self, rfq_id: uuid.UUID, body: RfqUpdate) -> RfqRead:
        self._require_edit()
        rfq = await self._rfq(rfq_id)
        if rfq.version != body.version:
            raise VersionConflict("The RFQ was changed by someone else; reload and try again")
        if rfq.status in ("awarded", "closed", "cancelled"):
            raise Conflict(f"An RFQ that is {rfq.status} can't be edited")
        changes: dict[str, Any] = body.model_dump(exclude_unset=True, exclude={"version"})
        if "equipment_categories" in changes:
            changes["equipment_categories"] = await self._check_codes(changes["equipment_categories"] or [])
        if "requirements" in changes:
            changes["requirements"] = [dict(r) for r in changes["requirements"] or []]
        for key, value in changes.items():
            setattr(rfq, key, value)
        rfq.version += 1
        await self.s.commit()
        await self.s.refresh(rfq)
        return await self._read(rfq)

    async def transition(self, rfq_id: uuid.UUID, target: str, body: Transition) -> RfqRead:
        self._require_edit()
        rfq = await self._rfq(rfq_id)
        if rfq.version != body.version:
            raise VersionConflict("The RFQ was changed by someone else; reload and try again")
        if target not in TRANSITIONS.get(rfq.status, set()):
            raise Conflict(f"An RFQ that is {rfq.status} can't be moved to {target}")
        if target == "issued":
            vendors = list(await self.s.scalars(select(RfqVendor).where(RfqVendor.rfq_id == rfq.id)))
            if not vendors:
                raise ValidationFailed("Add at least one vendor before issuing the RFQ")
            for v in vendors:
                if v.status == "shortlisted":
                    v.status = "invited"
            rfq.issued_at = datetime.now(UTC)
        record_audit(
            self.s,
            f"rfq.{target}",
            principal=self.principal,
            entity_type="rfq",
            entity_id=rfq.id,
            changes={"from": rfq.status, "to": target, "note": body.note},
        )
        rfq.status = target
        rfq.version += 1
        await self.s.commit()
        await self.s.refresh(rfq)
        return await self._read(rfq)

    async def award(self, rfq_id: uuid.UUID, body: AwardRequest) -> RfqRead:
        self._require_edit()
        rfq = await self._rfq(rfq_id)
        if rfq.version != body.version:
            raise VersionConflict("The RFQ was changed by someone else; reload and try again")
        if rfq.status not in ("issued", "evaluating"):
            raise Conflict(f"An RFQ that is {rfq.status} can't be awarded")
        vendors = {
            v.vendor_id: v for v in await self.s.scalars(select(RfqVendor).where(RfqVendor.rfq_id == rfq.id))
        }
        winner = vendors.get(body.vendor_id)
        if winner is None or winner.status != "quoted":
            raise ValidationFailed("Award to a vendor on this RFQ that has quoted")
        for v in vendors.values():
            if v.vendor_id == body.vendor_id:
                v.status = "awarded"
            elif v.status == "quoted":
                v.status = "lost"
        rfq.status, rfq.awarded_vendor_id, rfq.awarded_at = "awarded", body.vendor_id, datetime.now(UTC)
        rfq.version += 1
        record_audit(
            self.s,
            "rfq.awarded",
            principal=self.principal,
            entity_type="rfq",
            entity_id=rfq.id,
            changes={"vendor_id": str(body.vendor_id), "note": body.note},
        )
        await self.s.commit()
        await self.s.refresh(rfq)
        return await self._read(rfq)

    # ------------------------------------------------------------------ vendors

    async def vendors(self, rfq_id: uuid.UUID) -> list[RfqVendorRead]:
        rfq = await self._rfq(rfq_id)
        links = list(await self.s.scalars(select(RfqVendor).where(RfqVendor.rfq_id == rfq.id)))
        if not links:
            return []
        ids = [link.vendor_id for link in links]
        summaries, _ = await VendorQueryService(self.s, self.principal).search(
            VendorFilters(vendor_ids=tuple(ids), include_inactive=True), limit=len(ids), cursor=None
        )
        by_id = {v.id: v for v in summaries}
        cards: dict[uuid.UUID, Scorecard] = {}
        if rfq.project_id:
            for card in await self.s.scalars(
                select(Scorecard)
                .where(Scorecard.project_id == rfq.project_id, Scorecard.vendor_id.in_(ids))
                .order_by(Scorecard.updated_at)
            ):
                cards[card.vendor_id] = card
        order = {"awarded": 0, "quoted": 1, "invited": 2, "shortlisted": 3, "lost": 4, "declined": 5}
        rows = [
            RfqVendorRead(
                vendor=by_id[link.vendor_id],
                status=link.status,
                quote_amount=link.quote_amount,
                currency=link.currency,
                lead_time_weeks=link.lead_time_weeks,
                quote_received_at=link.quote_received_at,
                notes=link.notes,
                scorecard_id=cards[link.vendor_id].id if link.vendor_id in cards else None,
                score=cards[link.vendor_id].total_score if link.vendor_id in cards else None,
            )
            for link in links
            if link.vendor_id in by_id
        ]
        return sorted(
            rows,
            key=lambda r: (
                order[r.status],
                -(float(r.score) if r.score is not None else -1),
                r.vendor.display_name.lower(),
            ),
        )

    async def add_vendor(self, rfq_id: uuid.UUID, vendor_id: uuid.UUID) -> list[RfqVendorRead]:
        self._require_edit()
        rfq = await self._rfq(rfq_id)
        if rfq.status not in ("draft", "issued", "evaluating"):
            raise Conflict(f"Vendors can't be added to an RFQ that is {rfq.status}")
        if await self.s.get(Vendor, vendor_id) is None:
            raise NotFound("Vendor not found")
        exists = await self.s.scalar(
            select(RfqVendor.id).where(RfqVendor.rfq_id == rfq.id, RfqVendor.vendor_id == vendor_id)
        )
        if exists is None:
            status = "shortlisted" if rfq.status == "draft" else "invited"
            self.s.add(RfqVendor(tenant_id=self.tenant_id, rfq_id=rfq.id, vendor_id=vendor_id, status=status))
            await self.s.commit()
        return await self.vendors(rfq_id)

    async def update_vendor(
        self, rfq_id: uuid.UUID, vendor_id: uuid.UUID, body: RfqVendorUpdate
    ) -> list[RfqVendorRead]:
        self._require_edit()
        rfq = await self._rfq(rfq_id)
        if rfq.status in ("awarded", "closed", "cancelled"):
            raise Conflict(f"Quotes can't be changed on an RFQ that is {rfq.status}")
        link = await self.s.scalar(
            select(RfqVendor).where(RfqVendor.rfq_id == rfq.id, RfqVendor.vendor_id == vendor_id)
        )
        if link is None:
            raise NotFound("This vendor isn't on the RFQ")
        changes = body.model_dump(exclude_unset=True)
        if changes.get("status") in ("quoted", "declined") and rfq.status == "draft":
            raise Conflict("Issue the RFQ before recording quotes")
        if changes.get("quote_amount") is not None and link.status not in ("quoted",):
            changes.setdefault("status", "quoted")  # entering a price means a quote arrived
        for key, value in changes.items():
            setattr(link, key, value)
        if link.status == "quoted" and link.quote_received_at is None:
            link.quote_received_at = datetime.now(UTC)
        await self.s.commit()
        return await self.vendors(rfq_id)

    async def remove_vendor(self, rfq_id: uuid.UUID, vendor_id: uuid.UUID) -> None:
        self._require_edit()
        rfq = await self._rfq(rfq_id)
        if rfq.status != "draft":
            raise Conflict("Vendors can only be removed while the RFQ is a draft; record a decline instead")
        link = await self.s.scalar(
            select(RfqVendor).where(RfqVendor.rfq_id == rfq.id, RfqVendor.vendor_id == vendor_id)
        )
        if link is None:
            raise NotFound("This vendor isn't on the RFQ")
        await self.s.delete(link)
        await self.s.commit()
