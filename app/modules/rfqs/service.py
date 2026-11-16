"""RFQs: status workflow, vendors and quotes, comparison with scorecards.

Workflow: draft -> issued (needs at least one vendor; shortlisted vendors become invited) -> evaluating -> awarded
(to a vendor that quoted; the other quoted vendors become lost) -> closed. Anything not closed can be cancelled.
`rfq:manage` edits (tenant admins, managers, engineers); everyone in the organization can read. Every status change
is audited.
"""

import html
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import Conflict, Forbidden, NotFound, ValidationFailed, VersionConflict
from app.domain.permissions import P
from app.domain.principal import Principal
from app.infra.email import EmailSender, OutgoingEmail
from app.modules.audit.service import record_audit
from app.modules.projects.models import Project, ProjectVendor, Scorecard
from app.modules.rfqs.models import Rfq, RfqVendor
from app.modules.rfqs.schemas import (
    AwardRequest,
    EmailPreview,
    EmailRecipient,
    EmailSend,
    EmailSendResult,
    RfqCreate,
    RfqRead,
    RfqUpdate,
    RfqVendorRead,
    RfqVendorUpdate,
    Transition,
)
from app.modules.taxonomy.models import EquipmentCategory
from app.modules.tenants.models import Tenant
from app.modules.vendors.models import Vendor
from app.modules.vendors.service import VendorFilters, VendorQueryService

# status -> statuses it may move to by a plain transition (award has its own endpoint)
TRANSITIONS = {
    "draft": {"issued", "cancelled"},
    "issued": {"evaluating", "cancelled"},
    "evaluating": {"cancelled"},
    "awarded": {"closed"},
}


@dataclass(frozen=True)
class Sender:
    """The user sending an RFQ e-mail: their address receives the vendors' replies. Read by the caller outside the
    organization's row-level security (platform staff viewing as an organization have no user row in it)."""

    email: str | None
    name: str | None


class RfqService:
    def __init__(
        self, session: AsyncSession, principal: Principal, test_recipient: str | None = None
    ) -> None:
        if principal.tenant_id is None:
            raise Forbidden("RFQs belong to an organization; open the app as one to use them")
        self.s = session
        self.principal = principal
        self.tenant_id: uuid.UUID = principal.tenant_id
        self.test_recipient = test_recipient  # settings.rfq_test_recipient (testing only)

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
                emailed_at=link.emailed_at,
                emailed_to=link.emailed_to,
                test_recipient=self.test_recipient
                if self.test_recipient and not by_id[link.vendor_id].email
                else None,
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

    # ------------------------------------------------------------------ e-mail

    async def _email_parts(self, rfq: Rfq, sender: "Sender") -> tuple[str, str, str, str | None]:
        """Subject, plain text, HTML (with a {note} slot) and the reply-to address for an RFQ e-mail."""
        tenant = await self.s.get(Tenant, self.tenant_id)
        org = tenant.name if tenant else "our organization"
        project = await self.s.get(Project, rfq.project_id) if rfq.project_id else None
        reply_to = sender.email
        signer = sender.name or sender.email or org
        subject = f"Request for quotation {rfq.rfq_number}: {rfq.title} ({org})"
        reqs = [(r.get("text", ""), r.get("mandatory", True) is not False) for r in rfq.requirements or []]
        due = rfq.due_date.strftime("%d %B %Y") if rfq.due_date else None
        lines = [
            "Dear {vendor},",
            "",
            "{note}" + f"{org} invites you to quote for the following.",
            "",
            f"RFQ: {rfq.rfq_number}",
            f"Subject: {rfq.title}",
        ]
        if project:
            lines.append(f"Project: {project.name}")
        if due:
            lines.append(f"Quotation due: {due}")
        if rfq.description:
            lines += ["", rfq.description]
        if reqs:
            lines += ["", "Requirements:"]
            lines += [f"  - {t}{'' if m else ' (optional)'}" for t, m in reqs]
        lines += [
            "",
            "Please reply to this e-mail with your quotation: price and currency, delivery lead time in weeks, and "
            "any deviations from the requirements.",
            "",
            "Kind regards,",
            signer,
            org,
        ]
        text = "\n".join(lines)

        def esc(v: str) -> str:
            return html.escape(v, quote=False)

        rows = [("RFQ", rfq.rfq_number), ("Subject", rfq.title)]
        if project:
            rows.append(("Project", project.name))
        if due:
            rows.append(("Quotation due", due))
        table = "".join(
            f'<tr><td style="padding:4px 12px 4px 0;color:#555">{esc(k)}</td><td style="padding:4px 0"><b>{esc(v)}</b></td></tr>'
            for k, v in rows
        )
        req_html = (
            "<p><b>Requirements</b></p><ul>"
            + "".join(f"<li>{esc(t)}{'' if m else ' <i>(optional)</i>'}</li>" for t, m in reqs)
            + "</ul>"
            if reqs
            else ""
        )
        body_html = (
            '<div style="font-family:Arial,sans-serif;font-size:14px;color:#111">'
            "<p>Dear {vendor},</p>{note_html}"
            f"<p>{esc(org)} invites you to quote for the following.</p>"
            f'<table style="border-collapse:collapse">{table}</table>'
            + (f"<p>{esc(rfq.description)}</p>" if rfq.description else "")
            + req_html
            + "<p>Please reply to this e-mail with your quotation: price and currency, delivery lead time in weeks, "
            "and any deviations from the requirements.</p>"
            f"<p>Kind regards,<br>{esc(signer)}<br>{esc(org)}</p></div>"
        )
        return subject, text, body_html, reply_to

    async def email_preview(self, rfq_id: uuid.UUID, settings: Settings, sender: "Sender") -> EmailPreview:
        rfq = await self._rfq(rfq_id)
        subject, text, _, reply_to = await self._email_parts(rfq, sender)
        rows = await self.vendors(rfq_id)
        return EmailPreview(
            configured=settings.email_backend == "smtp",
            sender=settings.smtp_from,
            reply_to=reply_to,
            subject=subject,
            body=text.replace("{note}", "").replace("{vendor}", "<vendor name>"),
            recipients=[
                EmailRecipient(
                    vendor_id=r.vendor.id,
                    name=r.vendor.display_name,
                    email=r.vendor.email or settings.rfq_test_recipient,
                    test_address=not r.vendor.email and bool(settings.rfq_test_recipient),
                    status=r.status,
                    emailed_at=r.emailed_at,
                )
                for r in rows
            ],
        )

    async def send_email(
        self, rfq_id: uuid.UUID, body: EmailSend, settings: Settings, mailer: EmailSender, sender: "Sender"
    ) -> EmailSendResult:
        """One message per vendor (no vendor sees another), replies to the user sending it. A draft RFQ is issued
        by the send, so the vendors e-mailed become invited."""
        self._require_edit()
        rfq = await self._rfq(rfq_id)
        if rfq.status not in ("draft", "issued", "evaluating"):
            raise Conflict(f"An RFQ that is {rfq.status} can't be sent")
        subject, text, body_html, reply_to = await self._email_parts(rfq, sender)
        note = body.note.strip()
        note_text = f"{note}\n\n" if note else ""
        note_html = "".join(f"<p>{html.escape(p, quote=False)}</p>" for p in note.split("\n\n") if p.strip())
        rows = {r.vendor.id: r for r in await self.vendors(rfq_id)}
        result = EmailSendResult(sent=[], skipped=[], failed=[], issued=False)

        def message(to: str, vendor: str) -> OutgoingEmail:
            return OutgoingEmail(
                to=to,
                subject=subject,
                body=text.replace("{vendor}", vendor).replace("{note}", note_text),
                html=body_html.replace("{vendor}", html.escape(vendor, quote=False)).replace(
                    "{note_html}", note_html
                ),
                reply_to=reply_to,
                strict=True,
            )

        if body.test_to_me:
            if not reply_to:
                raise ValidationFailed("Your account has no e-mail address to send a test copy to")
            first = next((rows[v] for v in body.vendor_ids if v in rows), None)
            name = first.vendor.display_name if first else "Vendor"
            try:
                await mailer.send(replace(message(reply_to, name), subject=f"[TEST] {subject}"))
                result.sent.append(reply_to)
            except Exception as exc:  # the mail server refused it: say so
                result.failed.append(f"{reply_to}: {str(exc)[:120]}")
            return result

        links = {
            link.vendor_id: link
            for link in await self.s.scalars(
                select(RfqVendor).where(RfqVendor.rfq_id == rfq.id, RfqVendor.vendor_id.in_(body.vendor_ids))
            )
        }
        now = datetime.now(UTC)
        for vid in body.vendor_ids:
            row, link = rows.get(vid), links.get(vid)
            if row is None or link is None:
                result.skipped.append(f"{vid}: not on this RFQ")
                continue
            to = row.vendor.email
            msg: OutgoingEmail | None = None
            if to:
                msg = message(to, row.vendor.display_name)
            elif settings.rfq_test_recipient:
                # Testing: the vendor has no address, so the RFQ goes to the configured test mailbox, marked as such.
                to = settings.rfq_test_recipient
                msg = replace(message(to, row.vendor.display_name),
                              subject=f"[TEST for {row.vendor.display_name}] {subject}")  # fmt: skip
            if to is None or msg is None:
                result.skipped.append(f"{row.vendor.display_name}: no e-mail address on record")
                continue
            try:
                await mailer.send(msg)
            except Exception as exc:
                result.failed.append(f"{to}: {str(exc)[:120]}")
                continue
            result.sent.append(to)
            link.emailed_at, link.emailed_to = now, to
            if link.status == "shortlisted":
                link.status = "invited"
        if result.sent:
            if rfq.status == "draft":
                rfq.status, rfq.issued_at, result.issued = "issued", now, True
            rfq.version += 1
            record_audit(
                self.s,
                "rfq.emailed",
                principal=self.principal,
                entity_type="rfq",
                entity_id=rfq.id,
                changes={
                    "to": result.sent,
                    "skipped": result.skipped,
                    "failed": result.failed,
                    "issued": result.issued,
                    "note": note[:500],
                },
            )
        await self.s.commit()
        return result

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
