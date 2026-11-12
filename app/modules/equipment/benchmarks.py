"""Cost benchmarking and lead-time prediction (SOW 29.5) from the organization's own procurement records.

Grouped by equipment category (and everything under it) or by vendor: how long orders took (25th percentile,
median, 80th percentile -> "expect 18-26 weeks"), how often they arrived on time, and what a unit cost (PO or
quoted amount / line quantity, per currency, never converted). Prices only for callers with commercial:read.
Statistics, not promises: every row says how many records it rests on.
"""

import statistics
import uuid
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import SessionDep, require
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.equipment.models import ProcurementRecord, ProjectEquipment
from app.modules.taxonomy.models import EquipmentCategory
from app.modules.vendors.models import Vendor

router = APIRouter(tags=["project history"])
Reader = Annotated[Principal, Depends(require(P.EQUIPMENT_READ))]

MIN_FOR_RANGE = 3  # fewer records: a median, no range


class PriceStat(BaseModel):
    currency: str
    records: int
    median: Decimal
    low: Decimal
    high: Decimal


class Benchmark(BaseModel):
    key: str  # category code or vendor id
    label: str
    records: int  # procurement records (POs, quotations, requisitions) in the group
    lead_records: int
    lead_p25: float | None
    lead_median: float | None
    lead_p80: float | None
    expected: str  # "Expect 18-26 weeks (median 22, 7 orders)" or "Not enough data"
    on_time_rate: float | None
    prices: list[PriceStat] = []  # per unit, per currency; empty without commercial:read


class Benchmarks(BaseModel):
    group_by: Literal["category", "vendor"]
    rows: list[Benchmark]
    commercial_hidden: bool


def _quantile(values: list[float], q: float) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[round(q * 100) - 1]


def _expected(lead: list[float]) -> str:
    if not lead:
        return "No lead times recorded"
    med = statistics.median(lead)
    n = f"{len(lead)} order{'s' if len(lead) != 1 else ''}"
    if len(lead) < MIN_FOR_RANGE:
        return f"About {med:g} weeks ({n}; too few for a range)"
    return f"Expect {_quantile(lead, 0.25):g}-{_quantile(lead, 0.8):g} weeks (median {med:g}, {n})"


@router.get("/equipment/benchmarks", response_model=Benchmarks)
async def benchmarks(
    principal: Reader,
    session: SessionDep,
    group_by: Literal["category", "vendor"] = "category",
    category_code: str | None = None,
    vendor_id: uuid.UUID | None = None,
) -> Benchmarks:
    """Lead-time and unit-price benchmarks from your procurement records, by category or by vendor."""
    stmt = (
        select(ProcurementRecord, ProjectEquipment.quantity, EquipmentCategory.code, EquipmentCategory.name)
        .outerjoin(ProjectEquipment, ProjectEquipment.id == ProcurementRecord.equipment_id)
        .outerjoin(EquipmentCategory, EquipmentCategory.id == ProjectEquipment.category_id)
        .where(ProcurementRecord.status != "cancelled")
    )
    if category_code:
        stmt = stmt.where(
            (EquipmentCategory.code == category_code) | EquipmentCategory.code.like(f"{category_code}.%")
        )
    if vendor_id:
        stmt = stmt.where(ProcurementRecord.vendor_id == vendor_id)
    rows = (await session.execute(stmt)).all()
    vendor_names = dict(
        (
            await session.execute(
                select(Vendor.id, Vendor.display_name).where(
                    Vendor.id.in_({r[0].vendor_id for r in rows if r[0].vendor_id})
                )
            )
        ).all()
    )
    show_prices = principal.has(P.COMMERCIAL_READ)
    groups: dict[str, dict] = {}  # type: ignore[type-arg]
    for rec, qty, code, cat_name in rows:
        if group_by == "category":
            key, label = (code or "uncategorised"), (cat_name or "Uncategorised")
        else:
            key = str(rec.vendor_id) if rec.vendor_id else f"name:{rec.vendor_name.lower()}"
            label = (
                (vendor_names.get(rec.vendor_id) if rec.vendor_id else None)
                or rec.vendor_name
                or "Unknown vendor"
            )
        g = groups.setdefault(key, {"label": label, "n": 0, "lead": [], "on_time": [], "prices": {}})
        g["n"] += 1
        if rec.lead_time_weeks is not None:
            g["lead"].append(float(rec.lead_time_weeks))
        if rec.on_time is not None:
            g["on_time"].append(rec.on_time)
        if rec.amount is not None and rec.currency:
            g["prices"].setdefault(rec.currency, []).append(rec.amount / max(1, qty or 1))
    out: list[Benchmark] = []
    for key, g in groups.items():
        lead = sorted(g["lead"])
        enough = len(lead) >= MIN_FOR_RANGE
        out.append(
            Benchmark(
                key=key,
                label=g["label"],
                records=g["n"],
                lead_records=len(lead),
                lead_p25=_quantile(lead, 0.25) if enough else None,
                lead_median=statistics.median(lead) if lead else None,
                lead_p80=_quantile(lead, 0.8) if enough else None,
                expected=_expected(lead),
                on_time_rate=round(sum(g["on_time"]) / len(g["on_time"]), 3) if g["on_time"] else None,
                prices=[
                    PriceStat(
                        currency=cur,
                        records=len(vals),
                        median=Decimal(statistics.median(vals)).quantize(Decimal("0.01")),
                        low=min(vals).quantize(Decimal("0.01")),
                        high=max(vals).quantize(Decimal("0.01")),
                    )
                    for cur, vals in sorted(g["prices"].items())
                ]
                if show_prices
                else [],
            )
        )
    out.sort(key=lambda b: (-b.records, b.label))
    return Benchmarks(group_by=group_by, rows=out, commercial_hidden=not show_prices)
