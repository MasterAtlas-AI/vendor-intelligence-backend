"""Equipment technology trends (SOW 23.6) from the organization's own projects, by project year.

For one equipment category (and everything under it): how the typical ratings moved (median capacity, power,
pressure... per year), which design choices took over (text attributes such as driver type, material or seal plan,
and the standards specified), and which makers supplied it. Plain statistics over approved project history, each
with the number of lines it rests on; a trend needs at least two years with data.
"""

import statistics
from collections import Counter, defaultdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import SessionDep, require
from app.core.exceptions import NotFound
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.equipment.models import EquipmentAttribute, ProjectEquipment
from app.modules.projects.models import Project
from app.modules.taxonomy.models import EquipmentCategory
from app.modules.vendors.models import Vendor

router = APIRouter(tags=["project history"])
Reader = Annotated[Principal, Depends(require(P.EQUIPMENT_READ))]

MIN_LINES = 2  # per year, for a median to be shown
TOP_VALUES = 6


class YearCount(BaseModel):
    year: int
    lines: int
    projects: int


class NumericPoint(BaseModel):
    year: int
    lines: int
    median: float
    low: float
    high: float


class NumericTrend(BaseModel):
    key: str
    label: str
    unit: str
    points: list[NumericPoint]
    change: float | None  # last median / first median - 1, when two years have data


class ShareRow(BaseModel):
    value: str
    by_year: dict[int, float]  # share of that year's lines with a value for this field
    first_year: int
    lines: int


class ShareTrend(BaseModel):
    key: str  # attribute key, "standards" or "maker"
    label: str
    rows: list[ShareRow]


class CategoryOption(BaseModel):
    code: str
    name: str
    lines: int  # lines on dated projects


class Trends(BaseModel):
    categories: list[CategoryOption]  # the organization's categories with dated lines, most lines first
    category_code: str
    category_name: str
    years: list[YearCount]
    undated_lines: int  # lines on projects without a year
    numeric: list[NumericTrend]
    shares: list[ShareTrend]
    highlights: list[str]  # plain sentences: "Median power rose 85% (55 kW in 2014 to 102 kW in 2023)"


def _span(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, list) and len(value) == 2 and all(isinstance(v, int | float) for v in value):
        return float(max(value))  # a range is rated at its top
    return None


def _shares(label: str, key: str, values: dict[int, list[str]], totals: dict[int, int]) -> ShareTrend | None:
    """Per value, the share of each year's lines (with this field) that have it."""
    counts: Counter[str] = Counter(v for vs in values.values() for v in vs)
    if len(counts) < 2:
        return None
    rows = []
    for value, n in counts.most_common(TOP_VALUES):
        by_year = {
            y: round(sum(1 for v in vs if v == value) / totals[y], 3)
            for y, vs in sorted(values.items())
            if totals[y]
        }
        rows.append(
            ShareRow(
                value=value, by_year=by_year, lines=n, first_year=min(y for y, s in by_year.items() if s)
            )
        )
    return ShareTrend(key=key, label=label, rows=rows)


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:.3g}"


@router.get("/equipment/trends", response_model=Trends)
async def technology_trends(_: Reader, session: SessionDep, category_code: str | None = None) -> Trends:
    """How one equipment category changed over your projects' years: ratings, design choices, standards, makers.
    Without a category, the one with the most lines."""
    counted = (
        await session.execute(
            select(EquipmentCategory.code, EquipmentCategory.name, func.count())
            .join(ProjectEquipment, ProjectEquipment.category_id == EquipmentCategory.id)
            .join(Project, Project.id == ProjectEquipment.project_id)
            .where(Project.year.is_not(None), ProjectEquipment.verification != "archived")
            .group_by(EquipmentCategory.code, EquipmentCategory.name)
            .order_by(func.count().desc(), EquipmentCategory.name)
            .limit(300)
        )
    ).all()
    categories = [CategoryOption(code=c, name=n, lines=k) for c, n, k in counted]
    if category_code is None:
        if not categories:
            return Trends(categories=[], category_code="", category_name="", years=[], undated_lines=0,
                          numeric=[], shares=[], highlights=[])  # fmt: skip
        category_code = categories[0].code
    category = await session.scalar(select(EquipmentCategory).where(EquipmentCategory.code == category_code))
    if category is None:
        raise NotFound("Equipment category not found")
    rows = (
        await session.execute(
            select(ProjectEquipment, Project.year)
            .join(Project, Project.id == ProjectEquipment.project_id)
            .join(EquipmentCategory, EquipmentCategory.id == ProjectEquipment.category_id)
            .where(
                (EquipmentCategory.code == category_code) | EquipmentCategory.code.like(f"{category_code}.%"),
                ProjectEquipment.verification != "archived",
            )
        )
    ).all()
    defs = {a.key: a for a in await session.scalars(select(EquipmentAttribute))}
    maker_ids = {e.manufacturer_id or e.vendor_id for e, _ in rows} - {None}
    names = (
        dict(
            (
                await session.execute(select(Vendor.id, Vendor.display_name).where(Vendor.id.in_(maker_ids)))
            ).all()
        )
        if maker_ids
        else {}
    )

    lines: Counter[int] = Counter()
    projects: dict[int, set[Any]] = defaultdict(set)
    numbers: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    texts: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    undated = 0
    for e, year in rows:
        if year is None:
            undated += 1
            continue
        lines[year] += 1
        projects[year].add(e.project_id)
        for key, value in (e.attributes or {}).items():
            d = defs.get(key)
            if d is None:
                continue
            if d.data_type == "text":
                if isinstance(value, str) and value.strip() and key != "dimensions":
                    texts[key][year].append(" ".join(value.split())[:60].capitalize())
            elif (x := _span(value)) is not None:
                numbers[key][year].append(x)
        for std in e.standards or []:
            texts["standards"][year].append(std.strip()[:60])
        maker_id = e.manufacturer_id or e.vendor_id
        maker = (names.get(maker_id) if maker_id else None) or e.manufacturer_name or e.vendor_name
        if maker.strip():
            texts["maker"][year].append(" ".join(maker.split())[:80])

    numeric: list[NumericTrend] = []
    for key, measured in numbers.items():
        points = [
            NumericPoint(year=y, lines=len(v), median=statistics.median(v), low=min(v), high=max(v))
            for y, v in sorted(measured.items())
            if len(v) >= MIN_LINES
        ]
        if not points:
            continue
        change = points[-1].median / points[0].median - 1 if len(points) > 1 and points[0].median else None
        d = defs[key]
        numeric.append(NumericTrend(key=key, label=d.label, unit=d.unit, points=points, change=change))
    numeric.sort(key=lambda t: (-sum(p.lines for p in t.points), t.label))

    shares: list[ShareTrend] = []
    for key, written in texts.items():
        totals = {y: len(v) for y, v in written.items()}
        label = {"standards": "Standards specified", "maker": "Makers"}.get(key) or defs[key].label
        trend = _shares(label, key, written, totals)
        if trend:
            shares.append(trend)
    order = {"maker": 2, "standards": 1}
    shares.sort(key=lambda t: (order.get(t.key, 0), -sum(r.lines for r in t.rows)))

    highlights: list[str] = []
    for t in numeric:
        if t.change is not None and abs(t.change) >= 0.15:
            a, b = t.points[0], t.points[-1]
            verb = "rose" if t.change > 0 else "fell"
            unit = f" {t.unit}" if t.unit else ""
            highlights.append(
                f"Median {t.label.lower()} {verb} {abs(t.change):.0%} "
                f"({_fmt(a.median)}{unit} in {a.year} to {_fmt(b.median)}{unit} in {b.year})"
            )
    years = sorted(lines)
    if len(years) > 1:
        for s in shares:
            for r in s.rows:
                early = [r.by_year.get(y, 0) for y in years[: len(years) // 2]]
                late = [r.by_year.get(y, 0) for y in years[len(years) // 2 :]]
                if max(early) == 0 and max(late) >= 0.3 and r.lines >= MIN_LINES:
                    highlights.append(
                        f"{s.label}: {r.value} first appears in {r.first_year}, "
                        f"now on {r.by_year.get(years[-1], 0):.0%} of {years[-1]} lines"
                    )
    return Trends(
        categories=categories,
        category_code=category.code,
        category_name=category.name,
        years=[YearCount(year=y, lines=lines[y], projects=len(projects[y])) for y in years],
        undated_lines=undated,
        numeric=numeric,
        shares=shares,
        highlights=highlights[:8],
    )
