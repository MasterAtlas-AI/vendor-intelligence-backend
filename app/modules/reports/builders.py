"""Report contents. Each builder reads the knowledge base and the organization's own records (inside its RLS
session) and returns a ReportData; facts are loaded in bulk so an AVL export of thousands of vendors stays fast."""

import uuid
from collections import Counter
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound, ValidationFailed
from app.domain.enums import FactStatus, VendorStatus
from app.modules.projects.models import Project, ProjectVendor, Scorecard
from app.modules.reports.render import ReportData, Table
from app.modules.rfqs.models import Rfq, RfqVendor
from app.modules.taxonomy.models import CertificationType, EquipmentCategory, Industry, ServiceCategory
from app.modules.tenant_vendors.models import TenantVendorLink
from app.modules.vendors.models import (
    Vendor,
    VendorCertification,
    VendorEquipment,
    VendorIndustry,
    VendorService,
)

MAX_VENDORS = 5000


def pct(score: Decimal | None) -> str:
    return "" if score is None else f"{round(float(score) * 100)}%"


class Facts:
    """Approved facts of many vendors, by vendor id (category names, not codes)."""

    def __init__(self) -> None:
        self.equipment: dict[uuid.UUID, list[str]] = {}
        self.equipment_codes: dict[uuid.UUID, list[tuple[str, str]]] = {}  # (code, label)
        self.services: dict[uuid.UUID, list[str]] = {}
        self.certifications: dict[uuid.UUID, list[str]] = {}
        self.industries: dict[uuid.UUID, list[str]] = {}

    @classmethod
    async def load(cls, s: AsyncSession, ids: list[uuid.UUID]) -> "Facts":
        f = cls()
        if not ids:
            return f
        for vid, code, cat, name, model in await s.execute(
            select(
                VendorEquipment.vendor_id,
                EquipmentCategory.code,
                EquipmentCategory.name,
                VendorEquipment.name,
                VendorEquipment.model_series,
            )
            .join(EquipmentCategory, EquipmentCategory.id == VendorEquipment.category_id)
            .where(VendorEquipment.vendor_id.in_(ids), VendorEquipment.status == FactStatus.APPROVED)
        ):
            label = cat if name == cat else f"{cat}: {name}"
            if model and model not in label:
                label += f" ({model})"
            f.equipment.setdefault(vid, [])
            if cat not in f.equipment[vid]:
                f.equipment[vid].append(cat)
            f.equipment_codes.setdefault(vid, []).append((code, label))
        for vid, cat in await s.execute(
            select(VendorService.vendor_id, ServiceCategory.name)
            .join(ServiceCategory, ServiceCategory.id == VendorService.category_id)
            .where(VendorService.vendor_id.in_(ids), VendorService.status == FactStatus.APPROVED)
        ):
            f.services.setdefault(vid, [])
            if cat not in f.services[vid]:
                f.services[vid].append(cat)
        for vid, name in await s.execute(
            select(VendorCertification.vendor_id, CertificationType.name)
            .join(CertificationType, CertificationType.id == VendorCertification.certification_type_id)
            .where(VendorCertification.vendor_id.in_(ids), VendorCertification.status == FactStatus.APPROVED)
        ):
            f.certifications.setdefault(vid, []).append(name)
        for vid, name in await s.execute(
            select(VendorIndustry.vendor_id, Industry.name)
            .join(Industry, Industry.id == VendorIndustry.industry_id)
            .where(VendorIndustry.vendor_id.in_(ids))
        ):
            f.industries.setdefault(vid, []).append(name)
        return f


async def _vendors(s: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Vendor]:
    return {v.id: v for v in await s.scalars(select(Vendor).where(Vendor.id.in_(ids)))} if ids else {}


async def _links(
    s: AsyncSession, tenant_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, TenantVendorLink]:
    if not ids:
        return {}
    rows = await s.scalars(
        select(TenantVendorLink).where(
            TenantVendorLink.tenant_id == tenant_id, TenantVendorLink.vendor_id.in_(ids)
        )
    )
    return {link.vendor_id: link for link in rows}


async def vendor_comparison(s: AsyncSession, tenant_id: uuid.UUID, params: dict[str, Any]) -> ReportData:
    """Side by side: what each vendor makes and holds, your AVL status, and (for a project) stage and score."""
    stages: dict[uuid.UUID, str] = {}
    scores: dict[uuid.UUID, Decimal | None] = {}
    subtitle = ""
    if pid := params.get("project_id"):
        project = await s.get(Project, uuid.UUID(str(pid)))
        if project is None:
            raise NotFound("Project not found")
        subtitle = f"Project {project.name}"
        for vid, stage in await s.execute(
            select(ProjectVendor.vendor_id, ProjectVendor.stage).where(
                ProjectVendor.project_id == project.id, ProjectVendor.stage != "rejected"
            )
        ):
            stages[vid] = stage
        for card in await s.scalars(
            select(Scorecard).where(Scorecard.project_id == project.id).order_by(Scorecard.updated_at)
        ):
            scores[card.vendor_id] = card.total_score
        ids = list(stages)
    else:
        ids = [uuid.UUID(str(v)) for v in params.get("vendor_ids") or []]
        if not 1 <= len(ids) <= 50:
            raise ValidationFailed("Choose between 1 and 50 vendors, or a project")
    vendors = await _vendors(s, ids)
    facts = await Facts.load(s, ids)
    links = await _links(s, tenant_id, ids)
    order = sorted(vendors.values(), key=lambda v: (-(float(scores.get(v.id) or -1)), v.display_name.lower()))
    columns = [
        "Vendor",
        "Country",
        "Website",
        "Equipment",
        "Services",
        "Certifications",
        "Industries",
        "Your AVL status",
        "Data quality",
    ]
    if stages:
        columns += ["Stage", "Score"]
    rows = []
    for v in order:
        row: list[Any] = [
            v.display_name,
            v.hq_country,
            v.website,
            facts.equipment.get(v.id, []),
            facts.services.get(v.id, []),
            facts.certifications.get(v.id, []),
            facts.industries.get(v.id, []),
            links[v.id].avl_status.value.replace("_", " ") if v.id in links else "",
            pct(v.data_quality_score),
        ]
        if stages:
            row += [
                stages.get(v.id, ""),
                "" if scores.get(v.id) is None else f"{float(scores[v.id] or 0):.0f}",
            ]
        rows.append(row)
    widths = [2, 0.7, 1.6, 3, 2, 2, 1.6, 1.1, 0.9] + ([0.9, 0.7] if stages else [])
    return ReportData(
        title="Vendor comparison",
        subtitle=subtitle,
        facts=[("Vendors", str(len(rows)))],
        tables=[Table("Vendors", columns, rows, widths=widths)],
    )


async def market_scan(s: AsyncSession, tenant_id: uuid.UUID, params: dict[str, Any]) -> ReportData:
    """Who makes a kind of equipment (sub-categories included), where they are, and what they hold."""
    code = str(params.get("equipment_category") or "")
    category = await s.scalar(select(EquipmentCategory).where(EquipmentCategory.code == code))
    if category is None:
        raise ValidationFailed("Choose an equipment category")
    countries = [str(c).upper() for c in params.get("countries") or []]
    stmt = (
        select(VendorEquipment.vendor_id)
        .join(EquipmentCategory, EquipmentCategory.id == VendorEquipment.category_id)
        .join(Vendor, Vendor.id == VendorEquipment.vendor_id)
        .where(
            VendorEquipment.status == FactStatus.APPROVED,
            Vendor.status == VendorStatus.ACTIVE,
            (EquipmentCategory.code == code) | EquipmentCategory.code.like(f"{code}.%"),
        )
        .distinct()
        .limit(MAX_VENDORS)
    )
    if countries:
        stmt = stmt.where(Vendor.hq_country.in_(countries))
    ids = list(await s.scalars(stmt))
    vendors = await _vendors(s, ids)
    facts = await Facts.load(s, ids)
    links = await _links(s, tenant_id, ids)
    rows = []
    for v in sorted(vendors.values(), key=lambda v: (v.hq_country or "ZZ", v.display_name.lower())):
        matching = [
            label for c, label in facts.equipment_codes.get(v.id, []) if c == code or c.startswith(code + ".")
        ]
        rows.append(
            [
                v.display_name,
                v.hq_country,
                v.website,
                matching,
                facts.certifications.get(v.id, []),
                links[v.id].avl_status.value.replace("_", " ") if v.id in links else "",
                pct(v.data_quality_score),
            ]
        )
    by_country = Counter(v.hq_country or "Unknown" for v in vendors.values())
    certs = Counter(c for vid in ids for c in set(facts.certifications.get(vid, [])))
    on_avl = sum(1 for vid in ids if vid in links)
    return ReportData(
        title=f"Market scan: {category.name}",
        subtitle=f"Countries: {', '.join(countries)}" if countries else "All countries",
        facts=[
            ("Vendors", str(len(ids))),
            ("On your vendor list", str(on_avl)),
            ("Countries", str(len(by_country))),
        ],
        tables=[
            Table(
                "Vendors",
                [
                    "Vendor",
                    "Country",
                    "Website",
                    "Matching equipment",
                    "Certifications",
                    "Your AVL status",
                    "Data quality",
                ],
                rows,
                widths=[2, 0.7, 1.6, 3, 2.4, 1.1, 0.9],
            ),
            Table("By country", ["Country", "Vendors"], [[c, n] for c, n in by_country.most_common()]),
            Table(
                "Most common certifications",
                ["Certification", "Vendors"],
                [[c, n] for c, n in certs.most_common(15)],
            ),
        ],
    )


async def avl_export(s: AsyncSession, tenant_id: uuid.UUID, params: dict[str, Any]) -> ReportData:
    """The organization's own vendor list with its statuses, codes, tags and notes."""
    statuses = [str(x) for x in params.get("avl_status") or []]
    stmt = select(TenantVendorLink).where(TenantVendorLink.tenant_id == tenant_id).limit(MAX_VENDORS)
    if statuses:
        stmt = stmt.where(TenantVendorLink.avl_status.in_(statuses))
    links = {link.vendor_id: link for link in await s.scalars(stmt)}
    ids = list(links)
    vendors = await _vendors(s, ids)
    facts = await Facts.load(s, ids)
    rows = []
    for v in sorted(vendors.values(), key=lambda v: v.display_name.lower()):
        link = links[v.id]
        rows.append(
            [
                v.display_name,
                link.internal_vendor_code,
                link.avl_status.value.replace("_", " "),
                link.tags,
                v.hq_country,
                v.website,
                facts.equipment.get(v.id, []),
                facts.certifications.get(v.id, []),
                link.notes,
            ]
        )
    counts = Counter(link.avl_status.value.replace("_", " ") for link in links.values())
    return ReportData(
        title="Approved vendor list",
        subtitle=f"Statuses: {', '.join(statuses)}" if statuses else "All statuses",
        facts=[("Vendors", str(len(rows))), *((k.capitalize(), str(n)) for k, n in counts.most_common())],
        tables=[
            Table(
                "Vendors",
                [
                    "Vendor",
                    "Your code",
                    "AVL status",
                    "Tags",
                    "Country",
                    "Website",
                    "Equipment",
                    "Certifications",
                    "Notes",
                ],
                rows,
                widths=[2, 0.9, 0.9, 1.2, 0.6, 1.5, 2.6, 2, 1.8],
            )
        ],
    )


async def rfq_summary(s: AsyncSession, tenant_id: uuid.UUID, params: dict[str, Any]) -> ReportData:
    """One RFQ: header, every vendor's quote, lead time and score, the award and the requirements."""
    try:
        rfq = await s.get(Rfq, uuid.UUID(str(params.get("rfq_id"))))
    except ValueError:
        rfq = None
    if rfq is None:
        raise NotFound("RFQ not found")
    links = list(await s.scalars(select(RfqVendor).where(RfqVendor.rfq_id == rfq.id)))
    ids = [link.vendor_id for link in links]
    vendors = await _vendors(s, ids)
    scores: dict[uuid.UUID, Decimal | None] = {}
    project_name = None
    if rfq.project_id:
        project_name = await s.scalar(select(Project.name).where(Project.id == rfq.project_id))
        for card in await s.scalars(
            select(Scorecard).where(Scorecard.project_id == rfq.project_id).order_by(Scorecard.updated_at)
        ):
            scores[card.vendor_id] = card.total_score
    order = {"awarded": 0, "quoted": 1, "lost": 2, "invited": 3, "shortlisted": 4, "declined": 5}
    rows = []
    for link in sorted(links, key=lambda x: (order.get(x.status, 9), x.quote_amount or Decimal("1e18"))):
        v = vendors.get(link.vendor_id)
        rows.append(
            [
                v.display_name if v else "",
                v.hq_country if v else "",
                link.status,
                "" if link.quote_amount is None else f"{link.quote_amount:,.2f}",
                link.currency,
                "" if link.lead_time_weeks is None else str(link.lead_time_weeks),
                "" if scores.get(link.vendor_id) is None else f"{float(scores[link.vendor_id] or 0):.0f}",
                link.notes,
            ]
        )
    awarded = vendors[rfq.awarded_vendor_id].display_name if rfq.awarded_vendor_id in vendors else ""
    quoted = [link.quote_amount for link in links if link.quote_amount is not None]
    facts = [
        ("RFQ", rfq.rfq_number),
        ("Status", rfq.status),
        ("Project", project_name or "-"),
        ("Quotes due", str(rfq.due_date or "-")),
        ("Issued", f"{rfq.issued_at:%Y-%m-%d}" if rfq.issued_at else "-"),
        ("Awarded to", awarded or "-"),
        ("Quotes received", str(len(quoted))),
    ]
    if len(quoted) > 1:
        facts.append(("Quote range", f"{min(quoted):,.2f} - {max(quoted):,.2f}"))
    return ReportData(
        title=f"RFQ summary: {rfq.title}",
        subtitle=rfq.description[:300],
        facts=facts,
        tables=[
            Table(
                "Vendors and quotes",
                ["Vendor", "Country", "Status", "Quote", "Currency", "Lead time (weeks)", "Score", "Notes"],
                rows,
                widths=[2, 0.7, 0.9, 1, 0.7, 0.9, 0.6, 2.5],
            ),
            Table(
                "Requirements",
                ["Requirement", "Mandatory"],
                [[r.get("text", ""), "yes" if r.get("mandatory", True) else "no"] for r in rfq.requirements],
                widths=[5, 1],
            ),
        ],
    )


BUILDERS = {
    "vendor_comparison": vendor_comparison,
    "market_scan": market_scan,
    "avl": avl_export,
    "rfq_summary": rfq_summary,
}

TITLES = {
    "vendor_comparison": "Vendor comparison",
    "market_scan": "Market scan",
    "avl": "Approved vendor list",
    "rfq_summary": "RFQ summary",
}
