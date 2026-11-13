"""Supplier risk monitoring (SOW 29.9): a watchlist of the vendors on the organization's list, from signals already
in MasterAtlas: the vendor's standing in the knowledge base, its certificates, how its orders went, its technical
bid evaluations, how fresh its data is, and how much of the organization's equipment depends on it.

Rule-based and explained: every flag says why. Outside signals (sanctions lists, financial health, news) are not
read here; they would add to the same list of reasons.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Float, case, cast, func, select

from app.api.deps import SessionDep, require
from app.core.exceptions import ValidationFailed
from app.domain.enums import AvlStatus, FactStatus, VendorStatus
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.equipment.models import ProcurementRecord, ProjectEquipment
from app.modules.tenant_vendors.models import TenantVendorLink
from app.modules.vendors.models import Vendor, VendorCertification

router = APIRouter(tags=["vendors"])
Reader = Annotated[Principal, Depends(require(P.VENDOR_READ))]

Level = Literal["high", "medium", "low"]
EXPIRING_DAYS = 90
STALE_DAYS = 365
CONCENTRATION_SHARE = 0.15  # of the organization's equipment lines
CONCENTRATION_MIN_LINES = 10


class RiskFactor(BaseModel):
    key: str
    label: str
    detail: str
    weight: int  # 3 strong, 2 medium, 1 weak


class SupplierRisk(BaseModel):
    vendor_id: uuid.UUID
    name: str
    avl_status: AvlStatus
    level: Level
    score: int
    factors: list[RiskFactor]


class SupplierRiskPage(BaseModel):
    items: list[SupplierRisk]
    total: int  # vendors at or above the requested level
    counts: dict[str, int]  # high / medium / low over the whole list


def _level(score: int, strongest: int) -> Level:
    if strongest >= 3 or score >= 5:
        return "high"
    return "medium" if score >= 2 else "low"


@router.get("/supplier-risk", response_model=SupplierRiskPage)
async def supplier_risk(
    principal: Reader,
    session: SessionDep,
    min_level: Level = "medium",
    q: Annotated[str, Query(max_length=100)] = "",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> SupplierRiskPage:
    """The organization's vendors with risk signals, riskiest first, each with its reasons."""
    if principal.tenant_id is None:
        raise ValidationFailed(
            "Supplier risk is about an organization's vendors; open the app as one to use it"
        )
    today = date.today()
    links = (
        await session.execute(
            select(TenantVendorLink.vendor_id, TenantVendorLink.avl_status, Vendor.display_name, Vendor.status,
                   Vendor.last_verified_at)
            .join(Vendor, Vendor.id == TenantVendorLink.vendor_id)
        )
    ).all()  # fmt: skip
    if not links:
        return SupplierRiskPage(items=[], total=0, counts={"high": 0, "medium": 0, "low": 0})
    ids = [r[0] for r in links]
    factors: dict[uuid.UUID, list[RiskFactor]] = {i: [] for i in ids}

    def add(vid: uuid.UUID, key: str, label: str, detail: str, weight: int) -> None:
        factors[vid].append(RiskFactor(key=key, label=label, detail=detail, weight=weight))

    in_use = {AvlStatus.APPROVED, AvlStatus.CONDITIONAL}
    stale_before = datetime.now(UTC) - timedelta(days=STALE_DAYS)
    for vid, avl, _name, status, verified in links:
        if status != VendorStatus.ACTIVE and avl in in_use:
            add(vid, "inactive", "No longer active", f"Marked {status.value} in the knowledge base but still "
                f"{avl.value.replace('_', ' ')} on your list", 3)  # fmt: skip
        if avl in in_use and (verified is None or verified < stale_before):
            add(vid, "stale", "Data not re-checked", "Not verified in the last 12 months" if verified else
                "Never verified against its website", 1)  # fmt: skip

    # Certificates: expired, or expiring within 90 days.
    certs = (
        await session.execute(
            select(
                VendorCertification.vendor_id,
                func.count().filter(VendorCertification.valid_to < today),
                func.count().filter(
                    VendorCertification.valid_to.between(today, today + timedelta(days=EXPIRING_DAYS))
                ),
                func.min(VendorCertification.valid_to).filter(VendorCertification.valid_to >= today),
            )
            .where(VendorCertification.vendor_id.in_(ids), VendorCertification.status == FactStatus.APPROVED)
            .group_by(VendorCertification.vendor_id)
        )
    ).all()
    for vid, expired, expiring, next_expiry in certs:
        if expired:
            add(
                vid,
                "expired_certs",
                "Expired certificates",
                f"{expired} certificate(s) past their expiry date",
                3,
            )
        if expiring:
            add(vid, "expiring_certs", "Certificates expiring", f"{expiring} expire within {EXPIRING_DAYS} days "
                f"(next {next_expiry.isoformat() if next_expiry else 'soon'})", 2)  # fmt: skip

    # The organization's own orders, bid evaluations and equipment history: project-history data, so only for
    # callers who may read it (an API key or role with vendor:read alone sees the certificate and data factors).
    history = principal.has(P.EQUIPMENT_READ)
    orders = (
        (
            await session.execute(
                select(
                    ProcurementRecord.vendor_id,
                    func.count().filter(ProcurementRecord.on_time.is_not(None)),
                    func.count().filter(ProcurementRecord.on_time.is_(False)),
                    func.avg(cast(ProcurementRecord.performance_rating, Float)),
                    func.count().filter(ProcurementRecord.technical_status == "rejected"),
                )
                .where(ProcurementRecord.vendor_id.in_(ids))
                .group_by(ProcurementRecord.vendor_id)
            )
        ).all()
        if history
        else []
    )
    for vid, timed, late, rating, rejected in orders:
        if timed and late:
            share = late / timed
            if share >= 0.5 and timed >= 2:
                add(vid, "late", "Late deliveries", f"{late} of {timed} orders delivered late", 3)
            elif share >= 0.25:
                add(vid, "late", "Some late deliveries", f"{late} of {timed} orders delivered late", 2)
        if rating is not None and rating < 3:
            add(vid, "rating", "Low performance rating", f"Average {rating:.1f} of 5 on your orders", 2)
        if rejected:
            add(vid, "tbe_rejected", "Failed technical evaluation", f"Technically rejected in {rejected} of your "
                "bid evaluations", 2)  # fmt: skip

    # Dependence: one vendor behind a large share of the organization's equipment lines.
    supplier = case((ProjectEquipment.vendor_id.is_not(None), ProjectEquipment.vendor_id),
                    else_=ProjectEquipment.manufacturer_id)  # fmt: skip
    lines_total = (
        (await session.scalar(select(func.count()).select_from(ProjectEquipment)) or 0) if history else 0
    )
    if lines_total:
        rows = await session.execute(
            select(supplier, func.count())
            .where(supplier.in_(ids), ProjectEquipment.verification != "archived")
            .group_by(supplier)
        )
        for vid, n in rows.all():
            if n >= CONCENTRATION_MIN_LINES and n / lines_total >= CONCENTRATION_SHARE:
                add(vid, "concentration", "High dependence", f"Supplies {n} of your {lines_total} equipment lines "
                    f"({n / lines_total:.0%})", 2)  # fmt: skip

    ranked: list[SupplierRisk] = []
    for vid, avl, name, _status, _verified in links:
        f = factors[vid]
        score = sum(x.weight for x in f)
        if not f:
            continue
        ranked.append(SupplierRisk(vendor_id=vid, name=name, avl_status=avl, score=score,
                                   level=_level(score, max(x.weight for x in f)),
                                   factors=sorted(f, key=lambda x: -x.weight)))  # fmt: skip
    counts = {lvl: sum(1 for r in ranked if r.level == lvl) for lvl in ("high", "medium", "low")}
    order = {"high": 0, "medium": 1, "low": 2}
    shown = [r for r in ranked if order[r.level] <= order[min_level]]
    if q.strip():
        needle = q.strip().lower()
        shown = [r for r in shown if needle in r.name.lower()]
    shown.sort(key=lambda r: (order[r.level], -r.score, r.name.lower()))
    return SupplierRiskPage(items=shown[offset : offset + limit], total=len(shown), counts=counts)
