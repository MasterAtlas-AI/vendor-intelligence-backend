"""The organization's own project history: equipment lines, procurement, notes, attributes, MEL review, near-fit search.

Reading needs equipment:read (organization knowledge: every equipment line is readable by the organization, whatever
the project's membership); changing needs equipment:manage. Commercial values (PO amounts) need commercial:read.
"""

import hashlib
import re
import uuid
from collections import defaultdict
from typing import Any

import anyio
from sqlalchemy import Float, Integer, and_, case, cast, func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.exceptions import Conflict, Forbidden, NotFound, ValidationFailed
from app.domain.enums import AvlStatus, JobType, UploadKind
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.audit.service import record_audit
from app.modules.equipment.mel import COMMERCIAL_HEADER, attribute_defs, field_labels, read_mel, sheet_infos
from app.modules.equipment.models import (
    EquipmentAttribute,
    KnowledgeNote,
    MelRow,
    ProcurementRecord,
    ProjectEquipment,
    SavedSearch,
)
from app.modules.equipment.schemas import (
    AttributeDiff,
    AttributeRead,
    AttributeWrite,
    CategoryNode,
    EquipmentPage,
    EquipmentSearch,
    EquipmentUpdate,
    EquipmentWrite,
    Estimate,
    MelDecision,
    MelDecisionResult,
    MelRowRead,
    MelRows,
    MelRowUpdate,
    MelSheet,
    MelUploadRead,
    MelUploadSummary,
    NoteRead,
    NoteWrite,
    ProcurementRead,
    ProcurementWrite,
    ProjectEquipmentRead,
    ProjectRef,
    SavedSearchWrite,
    SimilarMatch,
    SimilarRequest,
    SimilarResult,
    VendorHistory,
)
from app.modules.equipment.units import canonical_unit
from app.modules.imports.models import Upload
from app.modules.imports.spreadsheet import TABLE_EXTENSIONS, extension
from app.modules.jobs.models import Job
from app.modules.projects.models import Project
from app.modules.taxonomy.models import EquipmentCategory
from app.modules.tenant_vendors.models import TenantVendorLink
from app.modules.vendors.models import Vendor, VendorEquipment

CANDIDATES = 3000  # records compared by a near-fit search


def _ref(p: Project) -> ProjectRef:
    return ProjectRef(
        id=p.id,
        name=p.name,
        code=p.code,
        client=p.client,
        facility_type=p.facility_type,
        country=p.country,
        year=p.year,
        location=p.location,
        kind=p.kind,
    )


def _span(value: Any) -> tuple[float, float] | None:
    """A stored attribute as (min, max), or None when it isn't numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value), float(value)
    if isinstance(value, list) and len(value) == 2 and all(isinstance(v, int | float) for v in value):
        return float(min(value)), float(max(value))
    return None


class EquipmentService:
    def __init__(self, session: AsyncSession, principal: Principal) -> None:
        self.s = session
        self.principal = principal
        self._tenant = principal.tenant_id  # None for platform staff outside an organization
        self._cats: dict[str, tuple[uuid.UUID, str]] | None = None

    @property
    def tenant_id(self) -> uuid.UUID:
        if self._tenant is None:
            raise Forbidden("Project history belongs to an organization; open the app as one to use it")
        return self._tenant

    # ------------------------------------------------------------------ helpers

    def _need(self, perm: P) -> None:
        self.tenant_id  # noqa: B018 - every project-history action needs an organization
        if not self.principal.has(perm):
            raise Forbidden(f"This needs the {perm.value} permission")

    async def _categories(self) -> dict[str, tuple[uuid.UUID, str]]:
        if self._cats is None:
            rows = await self.s.execute(
                select(EquipmentCategory.code, EquipmentCategory.id, EquipmentCategory.name)
            )
            self._cats = {code: (cid, name) for code, cid, name in rows.all()}
        return self._cats

    async def _category_id(self, code: str | None) -> uuid.UUID | None:
        if not code:
            return None
        found = (await self._categories()).get(code)
        if found is None:
            raise ValidationFailed(
                "Unknown equipment category", errors=[{"field": "category_code", "message": code}]
            )
        return found[0]

    async def _project(self, project_id: uuid.UUID) -> Project:
        project = await self.s.get(Project, project_id)
        if project is None:
            raise NotFound("Project not found")
        return project

    def _select(self) -> Any:
        return (
            select(
                ProjectEquipment, Project, EquipmentCategory.code, EquipmentCategory.name, Vendor.display_name
            )
            .join(Project, Project.id == ProjectEquipment.project_id)
            .outerjoin(EquipmentCategory, EquipmentCategory.id == ProjectEquipment.category_id)
            .outerjoin(Vendor, Vendor.id == ProjectEquipment.vendor_id)
        )

    @staticmethod
    def _read(row: Any) -> ProjectEquipmentRead:
        e, project, code, cat_name, vendor = row
        return ProjectEquipmentRead(
            id=e.id,
            project=_ref(project),
            category_code=code,
            category_name=cat_name,
            vendor_id=e.vendor_id,
            vendor_name=vendor or e.vendor_name,
            manufacturer_id=e.manufacturer_id,
            manufacturer_name=e.manufacturer_name,
            tag=e.tag,
            name=e.name,
            service=e.service,
            model=e.model,
            quantity=e.quantity,
            attributes=e.attributes,
            standards=e.standards,
            specs_text=e.specs_text,
            department=e.department,
            status=e.status,
            verification=e.verification,
            origin=e.origin,
            source_ref=e.source_ref,
            has_source_file=e.source_upload_id is not None,
            notes=e.notes,
            version=e.version,
            created_at=e.created_at,
        )

    async def _one(self, equipment_id: uuid.UUID) -> ProjectEquipmentRead:
        row = (await self.s.execute(self._select().where(ProjectEquipment.id == equipment_id))).first()
        if row is None:
            raise NotFound("Equipment not found")
        return self._read(row)

    # ------------------------------------------------------------------ equipment lines

    async def search(self, q: EquipmentSearch) -> EquipmentPage:
        self._need(P.EQUIPMENT_READ)
        stmt = self._select()
        conds: list[Any] = []
        if q.q.strip():
            like = f"%{q.q.strip()}%"
            conds.append(
                or_(
                    ProjectEquipment.tag.ilike(like),
                    ProjectEquipment.name.ilike(like),
                    ProjectEquipment.service.ilike(like),
                    ProjectEquipment.model.ilike(like),
                    ProjectEquipment.vendor_name.ilike(like),
                    ProjectEquipment.manufacturer_name.ilike(like),
                    Vendor.display_name.ilike(like),
                    Project.name.ilike(like),
                )
            )
        if q.project_ids:
            conds.append(ProjectEquipment.project_id.in_(q.project_ids))
        if q.category_code:
            conds.append(
                or_(
                    EquipmentCategory.code == q.category_code,
                    EquipmentCategory.code.like(f"{q.category_code}.%"),
                )
            )
        if q.vendor_id:
            conds.append(
                or_(
                    ProjectEquipment.vendor_id == q.vendor_id, ProjectEquipment.manufacturer_id == q.vendor_id
                )
            )
        if q.service.strip():
            conds.append(ProjectEquipment.service.ilike(f"%{q.service.strip()}%"))
        if q.statuses:
            conds.append(ProjectEquipment.status.in_(q.statuses))
        if q.countries:
            conds.append(Project.country.in_([c.upper() for c in q.countries]))
        if q.facility_types:
            conds.append(or_(*(Project.facility_type.ilike(f"%{f}%") for f in q.facility_types)))
        if q.location:
            conds.append(Project.location == q.location)
        if q.year_from:
            conds.append(Project.year >= q.year_from)
        if q.year_to:
            conds.append(Project.year <= q.year_to)
        if q.standard:
            conds.append(func.array_to_string(ProjectEquipment.standards, " ").ilike(f"%{q.standard}%"))
        if q.material:
            conds.append(ProjectEquipment.attributes["material"].astext.ilike(f"%{q.material}%"))
        if q.verification:
            conds.append(ProjectEquipment.verification.in_(q.verification))
        elif not q.include_archived:
            conds.append(ProjectEquipment.verification != "archived")
        if q.department:
            conds.append(ProjectEquipment.department == q.department)
        for i, (key, bounds) in enumerate(q.ranges.items()):
            lo, hi = [*bounds, None, None][:2]
            value = f"project_equipment.attributes -> :k{i}"  # a number, or [min, max]
            low = text(
                f"(CASE jsonb_typeof({value}) WHEN 'number' THEN ({value})::text::float "
                f"WHEN 'array' THEN ({value} ->> 0)::float END)"
            ).bindparams(**{f"k{i}": key})
            high = text(
                f"(CASE jsonb_typeof({value}) WHEN 'number' THEN ({value})::text::float "
                f"WHEN 'array' THEN ({value} ->> 1)::float END)"
            ).bindparams(**{f"k{i}": key})
            if lo is not None:
                conds.append(cast(high, Float) >= lo)
            if hi is not None:
                conds.append(cast(low, Float) <= hi)
        if conds:
            stmt = stmt.where(and_(*conds))
        total = await self.s.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
        rows = await self.s.execute(
            stmt.order_by(Project.year.desc().nulls_last(), ProjectEquipment.tag, ProjectEquipment.name)
            .limit(q.limit)
            .offset(q.offset)
        )
        return EquipmentPage(items=[self._read(r) for r in rows.all()], total=int(total))

    async def get(self, equipment_id: uuid.UUID) -> ProjectEquipmentRead:
        self._need(P.EQUIPMENT_READ)
        return await self._one(equipment_id)

    async def create(self, body: EquipmentWrite) -> ProjectEquipmentRead:
        self._need(P.EQUIPMENT_MANAGE)
        await self._project(body.project_id)
        data = body.model_dump(exclude={"category_code"})
        e = ProjectEquipment(
            tenant_id=self.tenant_id, category_id=await self._category_id(body.category_code), **data
        )
        self.s.add(e)
        await self.s.flush()
        record_audit(
            self.s,
            "equipment.created",
            principal=self.principal,
            entity_type="project_equipment",
            entity_id=e.id,
            changes={"name": e.name, "project_id": str(e.project_id)},
        )
        await self.s.commit()
        return await self._one(e.id)

    async def source_file(self, equipment_id: uuid.UUID) -> Upload:
        self._need(P.EQUIPMENT_READ)
        e = await self.s.get(ProjectEquipment, equipment_id)
        if e is None:
            raise NotFound("Equipment not found")
        if e.source_upload_id is None:
            raise NotFound("This record has no uploaded source file")
        upload = await self.s.scalar(
            select(Upload).where(Upload.id == e.source_upload_id).options(undefer(Upload.data))
        )
        if upload is None:
            raise NotFound("The source file is no longer stored")
        if not self.principal.has(P.COMMERCIAL_READ) and await self.s.scalar(
            select(MelRow.id)
            .where(MelRow.upload_id == upload.id, MelRow.parsed["amount"].astext.is_not(None))
            .limit(1)
        ):
            raise Forbidden("This file includes prices; downloading it needs access to commercial data")
        return upload

    async def update(self, equipment_id: uuid.UUID, body: EquipmentUpdate) -> ProjectEquipmentRead:
        self._need(P.EQUIPMENT_MANAGE)
        e = await self.s.get(ProjectEquipment, equipment_id)
        if e is None:
            raise NotFound("Equipment not found")
        if e.version != body.version:
            raise Conflict("Someone else changed this record; reload and try again", code="version_conflict")
        changes = body.model_dump(exclude_unset=True, exclude={"version", "category_code"})
        if "category_code" in body.model_fields_set:
            e.category_id = await self._category_id(body.category_code)
            changes["category_code"] = body.category_code
        for key, value in changes.items():
            if key != "category_code":
                setattr(e, key, value)
        e.version += 1
        record_audit(
            self.s,
            "equipment.updated",
            principal=self.principal,
            entity_type="project_equipment",
            entity_id=e.id,
            changes={k: str(v)[:200] for k, v in changes.items()},
        )
        await self.s.commit()
        return await self._one(e.id)

    async def archive(self, equipment_id: uuid.UUID) -> None:
        """Archived / superseded (SOW 19): kept for traceability, hidden from searches by default."""
        self._need(P.EQUIPMENT_MANAGE)
        e = await self.s.get(ProjectEquipment, equipment_id)
        if e is None:
            raise NotFound("Equipment not found")
        e.verification = "archived"
        e.version += 1
        record_audit(
            self.s,
            "equipment.archived",
            principal=self.principal,
            entity_type="project_equipment",
            entity_id=e.id,
        )
        await self.s.commit()

    # ------------------------------------------------------------------ near-fit (SOW 12, 13)

    async def similar(self, req: SimilarRequest) -> SimilarResult:
        self._need(P.EQUIPMENT_READ)
        cats = await self._categories()
        if req.category_code and req.category_code not in cats:
            raise ValidationFailed("Unknown equipment category")
        defs = {d.key: d for d in await attribute_defs(self.s)}
        family = (
            req.category_code.rsplit(".", 1)[0]
            if req.category_code and "." in req.category_code
            else req.category_code
        )

        def category_score(code: str | None) -> float:
            if not req.category_code:
                return 1.0
            if not code:
                return 0.0
            if code == req.category_code:
                return 1.0
            if code.startswith(req.category_code + "."):
                return 0.9
            if family and (code == family or code.startswith(family + ".")):
                return 0.7
            return 0.4 if code.split(".")[0] == req.category_code.split(".")[0] else 0.0

        def in_family(column: Any) -> Any:
            if not family:
                return True
            root = family.split(".")[0]
            return or_(column == root, column.like(f"{root}.%"))

        matches: list[SimilarMatch] = []
        closeness = (
            case(
                (EquipmentCategory.code == req.category_code, 0),
                (EquipmentCategory.code.like(f"{req.category_code}.%"), 1),
                (EquipmentCategory.code.like(f"{family or req.category_code}%"), 2),
                else_=3,
            )
            if req.category_code
            else literal(0)
        )
        history = await self.s.execute(
            self._select()
            .where(ProjectEquipment.verification != "archived", in_family(EquipmentCategory.code))
            .order_by(closeness, ProjectEquipment.created_at.desc(), ProjectEquipment.id)
            .limit(CANDIDATES)
        )
        records = history.all()
        for row in records:
            e, project, code, cat_name, vendor = row
            m = self._compare(
                req,
                e.attributes,
                e.standards,
                category_score(code),
                defs,
                e.service or e.name,
                project.location if project is not None else None,
            )
            if m is None:
                continue
            score, exact, diffs = m
            matches.append(
                SimilarMatch(
                    source="spe_history",
                    match="exact" if exact else "comparable",
                    score=score,
                    equipment_id=e.id,
                    product_id=None,
                    vendor_id=e.vendor_id,
                    vendor_name=vendor or e.vendor_name,
                    name=e.name,
                    model=e.model,
                    category_code=code,
                    category_name=cat_name,
                    project=_ref(project),
                    standards=e.standards,
                    diffs=diffs,
                    verification=e.verification,
                )
            )
        if req.include_catalogue:
            catalogue = await self.s.execute(
                select(VendorEquipment, EquipmentCategory.code, EquipmentCategory.name, Vendor.display_name)
                .join(EquipmentCategory, EquipmentCategory.id == VendorEquipment.category_id)
                .join(Vendor, Vendor.id == VendorEquipment.vendor_id)
                .where(VendorEquipment.status == "approved", in_family(EquipmentCategory.code))
                .limit(CANDIDATES)
            )
            for p, code, cat_name, vendor in catalogue.all():
                m = self._compare(req, p.attributes or {}, p.standards, category_score(code), defs, p.name)
                if m is None:
                    continue
                score, exact, diffs = m
                matches.append(
                    SimilarMatch(
                        source="catalogue",
                        match="exact" if exact else "comparable",
                        score=score,
                        equipment_id=None,
                        product_id=p.id,
                        vendor_id=p.vendor_id,
                        vendor_name=vendor,
                        name=p.name,
                        model=p.model_series or "",
                        category_code=code,
                        category_name=cat_name,
                        project=None,
                        standards=p.standards,
                        diffs=diffs,
                        verification="public",
                    )
                )
        matches.sort(key=lambda x: (-x.score, x.source != "spe_history"))
        estimates = self._estimate(req, [r[0] for r in records], [r[2] for r in records], defs)
        return SimilarResult(matches=matches[: req.limit], estimates=estimates)

    @staticmethod
    def _compare(
        req: SimilarRequest,
        attrs: dict[str, Any],
        standards: list[str],
        cat: float,
        defs: dict[str, Any],
        service: str = "",
        location: str | None = None,
    ) -> tuple[float, bool, list[AttributeDiff]] | None:
        if req.category_code and cat == 0:
            return None
        diffs: list[AttributeDiff] = []
        attr_scores: list[float] = []
        for key, target in req.attributes.items():
            span = _span(attrs.get(key))
            if span is None:
                diffs.append(
                    AttributeDiff(
                        key=key, requested=target, found=None, difference_pct=None, note="not on record"
                    )
                )
                attr_scores.append(0.0)
                continue
            lo, hi = span
            found: float | list[float] = lo if lo == hi else [lo, hi]
            if lo <= target <= hi:
                pct, note = 0.0, "matches" if lo == hi else "within range"
            else:
                edge = lo if target < lo else hi
                if not target:
                    gap = edge - target
                    diffs.append(AttributeDiff(key=key, requested=target, found=found, difference_pct=None,
                                               note=f"{gap:+g} from the requested 0"))  # fmt: skip
                    attr_scores.append(max(0.0, 1 - abs(gap) / max(abs(edge), 1.0)))
                    continue
                pct = round((edge - target) / abs(target) * 100, 1)
                note = f"{'+' if pct > 0 else ''}{pct}% {'above' if pct > 0 else 'below'}"
            diffs.append(AttributeDiff(key=key, requested=target, found=found, difference_pct=pct, note=note))
            attr_scores.append(max(0.0, 1 - abs(pct) / 100))
        parts: list[tuple[float, float]] = [(0.35, cat)]
        if attr_scores:
            parts.append((0.5, sum(attr_scores) / len(attr_scores)))
        if req.standards:
            have = " ".join(standards).lower().replace(" ", "")
            hit = sum(1 for s in req.standards if s.lower().replace(" ", "") in have) / len(req.standards)
            parts.append((0.15, hit))
        if req.service.strip():  # SOW 13.1: the same duty ("seawater", "crude", "produced water")
            parts.append((0.15, _service_overlap(req.service, service)))
        if req.material.strip():  # SOW 13.2: "Duplex" ~ "DSS 22Cr Duplex"; "SS316" ~ "SS 316L"
            parts.append((0.1, _text_overlap(req.material, str(attrs.get("material") or ""))))
        if req.package_configuration.strip():
            parts.append(
                (0.1, _text_overlap(req.package_configuration, str(attrs.get("package_configuration") or "")))
            )
        if (
            req.location
        ):  # offshore duty: equipment proven offshore scores higher (catalogue products: unknown)
            parts.append((0.1, 1.0 if location == req.location else 0.5 if location is None else 0.0))
        score = round(sum(w * v for w, v in parts) / sum(w for w, _ in parts), 3)
        if score < 0.3:
            return None
        exact = cat == 1.0 and all(d.difference_pct is not None and abs(d.difference_pct) <= 5 for d in diffs)
        return score, exact, diffs

    @staticmethod
    def _estimate(
        req: SimilarRequest, records: list[ProjectEquipment], codes: list[str | None], defs: dict[str, Any]
    ) -> list[Estimate]:
        """Values asked for in `estimate`, read off the line through the nearest historical records of the same
        category, using the first requested attribute as the driver. Always labelled as not verified."""
        if not req.estimate or not req.attributes:
            return []
        driver, target = next(iter(req.attributes.items()))
        out: list[Estimate] = []
        for key in req.estimate:
            points: list[tuple[float, float, str]] = []
            for e, code in zip(records, codes, strict=True):
                if req.category_code and code != req.category_code:
                    continue
                x, y = _span(e.attributes.get(driver)), _span(e.attributes.get(key))
                if x and y and x[0] == x[1] and y[0] == y[1]:
                    points.append((x[0], y[0], f"{e.tag or e.name} ({e.model or 'no model'})"))
            if not points:
                continue
            points.sort()
            at = [p for p in points if p[0] == target]
            if at:
                d = defs.get(key)
                out.append(
                    Estimate(
                        key=key,
                        label=d.label if d else key,
                        value=round(sum(p[1] for p in at) / len(at), 3),
                        unit=d.unit if d else "",
                        method="nearest",
                        driver=driver,
                        basis=[p[2] for p in at][:3],
                    )
                )
                continue
            below = [p for p in points if p[0] <= target]
            above = [p for p in points if p[0] >= target]
            if below and above and below[-1][0] != above[0][0]:
                (x0, y0, a), (x1, y1, b) = below[-1], above[0]
                value, method, basis = y0 + (y1 - y0) * (target - x0) / (x1 - x0), "interpolated", [a, b]
            elif len(points) >= 2:
                (x0, y0, a), (x1, y1, b) = points[:2] if not below else points[-2:]
                if x1 == x0:
                    value, method, basis = (y0 + y1) / 2, "nearest", [a, b]
                else:
                    value, method, basis = y0 + (y1 - y0) * (target - x0) / (x1 - x0), "extrapolated", [a, b]
            else:
                value, method, basis = points[0][1], "nearest", [points[0][2]]
            d = defs.get(key)
            out.append(
                Estimate(
                    key=key,
                    label=d.label if d else key,
                    value=round(value, 3),
                    unit=d.unit if d else "",
                    method=method,
                    driver=driver,
                    basis=basis,
                )
            )
        return out

    # ------------------------------------------------------------------ procurement records

    def _procurement_read(self, r: ProcurementRecord, project_name: str = "") -> ProcurementRead:
        out = ProcurementRead.model_validate(r)
        out.project_name = project_name
        if not self.principal.has(P.COMMERCIAL_READ):
            out.amount, out.currency, out.commercial_hidden = None, None, True
        return out

    async def procurement(
        self, vendor_id: uuid.UUID | None, project_id: uuid.UUID | None, equipment_id: uuid.UUID | None
    ) -> list[ProcurementRead]:
        self._need(P.EQUIPMENT_READ)
        stmt = select(ProcurementRecord, Project.name).join(
            Project, Project.id == ProcurementRecord.project_id
        )
        if vendor_id:
            stmt = stmt.where(ProcurementRecord.vendor_id == vendor_id)
        if project_id:
            stmt = stmt.where(ProcurementRecord.project_id == project_id)
        if equipment_id:
            stmt = stmt.where(ProcurementRecord.equipment_id == equipment_id)
        rows = await self.s.execute(stmt.order_by(ProcurementRecord.po_date.desc().nulls_last()).limit(500))
        return [self._procurement_read(r, name) for r, name in rows.all()]

    async def _check_refs(
        self, project_id: uuid.UUID | None, equipment_id: uuid.UUID | None, vendor_id: uuid.UUID | None
    ) -> None:
        """References are checked here, under row-level security: a foreign key alone would accept another
        organization's line (Postgres checks foreign keys without RLS)."""
        if equipment_id:
            line = await self.s.get(ProjectEquipment, equipment_id)
            if line is None:
                raise NotFound("Equipment not found")
            if project_id and line.project_id != project_id:
                raise ValidationFailed("That equipment line is not on this project")
        if vendor_id and await self.s.get(Vendor, vendor_id) is None:
            raise NotFound("Vendor not found")

    @staticmethod
    def _derive(r: ProcurementRecord) -> None:
        if r.lead_time_weeks is None and r.po_date and r.delivered_date:
            r.lead_time_weeks = max(0, round((r.delivered_date - r.po_date).days / 7))
        r.on_time = (r.delivered_date <= r.promised_date) if r.delivered_date and r.promised_date else None

    async def create_procurement(self, body: ProcurementWrite) -> ProcurementRead:
        self._need(P.EQUIPMENT_MANAGE)
        project = await self._project(body.project_id)
        await self._check_refs(body.project_id, body.equipment_id, body.vendor_id)
        r = ProcurementRecord(tenant_id=self.tenant_id, **body.model_dump())
        if r.currency:
            r.currency = r.currency.upper()
        self._derive(r)
        self.s.add(r)
        await self.s.flush()
        record_audit(
            self.s,
            "procurement.created",
            principal=self.principal,
            entity_type="procurement_record",
            entity_id=r.id,
            changes={"po_number": r.po_number},
        )
        await self.s.commit()
        await self.s.refresh(r)
        return self._procurement_read(r, project.name)

    async def update_procurement(self, record_id: uuid.UUID, body: ProcurementWrite) -> ProcurementRead:
        self._need(P.EQUIPMENT_MANAGE)
        r = await self.s.get(ProcurementRecord, record_id)
        if r is None:
            raise NotFound("Procurement record not found")
        await self._project(body.project_id)
        await self._check_refs(body.project_id, body.equipment_id, body.vendor_id)
        data = body.model_dump()
        if not self.principal.has(P.COMMERCIAL_READ):
            data.pop("amount"), data.pop("currency")  # can't see them, so can't overwrite them
        for key, value in data.items():
            setattr(r, key, value)
        r.lead_time_weeks = body.lead_time_weeks
        self._derive(r)
        record_audit(
            self.s,
            "procurement.updated",
            principal=self.principal,
            entity_type="procurement_record",
            entity_id=r.id,
        )
        await self.s.commit()
        await self.s.refresh(r)
        return self._procurement_read(r, (await self._project(r.project_id)).name)

    async def delete_procurement(self, record_id: uuid.UUID) -> None:
        self._need(P.EQUIPMENT_MANAGE)
        r = await self.s.get(ProcurementRecord, record_id)
        if r is None:
            raise NotFound("Procurement record not found")
        record_audit(
            self.s,
            "procurement.deleted",
            principal=self.principal,
            entity_type="procurement_record",
            entity_id=r.id,
            changes={"po_number": r.po_number},
        )
        await self.s.delete(r)
        await self.s.commit()

    # ------------------------------------------------------------------ knowledge notes

    async def _note_read(self, n: KnowledgeNote) -> NoteRead:
        project = await self.s.get(Project, n.project_id) if n.project_id else None
        code = next((c for c, (cid, _) in (await self._categories()).items() if cid == n.category_id), None)
        return NoteRead(
            id=n.id,
            kind=n.kind,
            title=n.title,
            body=n.body,
            department=n.department,
            project_id=n.project_id,
            project_name=project.name if project else None,
            vendor_id=n.vendor_id,
            equipment_id=n.equipment_id,
            category_code=code,
            verification=n.verification,
            created_at=n.created_at,
        )

    async def notes(
        self,
        vendor_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        equipment_id: uuid.UUID | None,
        kind: str | None,
    ) -> list[NoteRead]:
        self._need(P.EQUIPMENT_READ)
        stmt = select(KnowledgeNote)
        if vendor_id:
            stmt = stmt.where(KnowledgeNote.vendor_id == vendor_id)
        if project_id:
            stmt = stmt.where(KnowledgeNote.project_id == project_id)
        if equipment_id:
            stmt = stmt.where(KnowledgeNote.equipment_id == equipment_id)
        if kind:
            stmt = stmt.where(KnowledgeNote.kind == kind)
        notes = await self.s.scalars(stmt.order_by(KnowledgeNote.created_at.desc()).limit(300))
        return [await self._note_read(n) for n in notes]

    async def create_note(self, body: NoteWrite) -> NoteRead:
        # Anyone who can read the history may record a lesson or feedback; it stays "user-entered" until verified.
        self._need(P.EQUIPMENT_READ)
        data = body.model_dump(exclude={"category_code"})
        if not self.principal.has(P.EQUIPMENT_MANAGE):
            data["verification"] = "user_entered"
        if body.project_id:
            await self._project(body.project_id)
        await self._check_refs(body.project_id, body.equipment_id, body.vendor_id)
        n = KnowledgeNote(
            tenant_id=self.tenant_id, category_id=await self._category_id(body.category_code), **data
        )
        self.s.add(n)
        await self.s.flush()
        record_audit(
            self.s,
            "note.created",
            principal=self.principal,
            entity_type="knowledge_note",
            entity_id=n.id,
            changes={"kind": n.kind, "title": n.title},
        )
        await self.s.commit()
        await self.s.refresh(n)
        return await self._note_read(n)

    async def delete_note(self, note_id: uuid.UUID) -> None:
        n = await self.s.get(KnowledgeNote, note_id)
        if n is None:
            raise NotFound("Note not found")
        if not (self.principal.has(P.EQUIPMENT_MANAGE) or n.created_by == self.principal.user_id):
            raise Forbidden("Only its author or a data administrator can delete a note")
        record_audit(
            self.s, "note.deleted", principal=self.principal, entity_type="knowledge_note", entity_id=n.id
        )
        await self.s.delete(n)
        await self.s.commit()

    # ------------------------------------------------------------------ vendor history

    async def vendor_history(self, vendor_id: uuid.UUID) -> VendorHistory:
        self._need(P.EQUIPMENT_READ)
        used = or_(ProjectEquipment.vendor_id == vendor_id, ProjectEquipment.manufacturer_id == vendor_id)
        projects = list(
            await self.s.scalars(
                select(Project)
                .where(
                    or_(
                        Project.id.in_(select(ProjectEquipment.project_id).where(used)),
                        Project.id.in_(
                            select(ProcurementRecord.project_id).where(
                                ProcurementRecord.vendor_id == vendor_id
                            )
                        ),
                    )
                )
                .order_by(Project.year.desc().nulls_last(), Project.name)
            )
        )
        count = await self.s.scalar(
            select(func.count())
            .select_from(ProjectEquipment)
            .where(used, ProjectEquipment.verification != "archived")
        )
        categories = list(
            await self.s.scalars(
                select(EquipmentCategory.name)
                .join(ProjectEquipment, ProjectEquipment.category_id == EquipmentCategory.id)
                .where(used)
                .distinct()
                .limit(20)
            )
        )
        po = (
            await self.s.execute(
                select(
                    func.count().filter(ProcurementRecord.record_type == "po"),
                    func.avg(ProcurementRecord.lead_time_weeks),
                    func.avg(cast(cast(ProcurementRecord.on_time, Integer), Float)),
                    func.avg(ProcurementRecord.performance_rating),
                    func.count().filter(ProcurementRecord.record_type == "quotation"),
                    func.count().filter(ProcurementRecord.technical_status.in_(("accepted", "conditional"))),
                    func.count().filter(ProcurementRecord.technical_status == "rejected"),
                ).where(ProcurementRecord.vendor_id == vendor_id)
            )
        ).one()
        notes = await self.notes(vendor_id, None, None, None)

        def rnd(v: Any, n: int = 2) -> float | None:
            return round(float(v), n) if v is not None else None

        return VendorHistory(
            vendor_id=vendor_id,
            projects=[_ref(p) for p in projects],
            equipment_count=int(count or 0),
            categories=categories,
            purchase_orders=int(po[0] or 0),
            avg_lead_time_weeks=rnd(po[1], 1),
            on_time_rate=rnd(po[2]),
            avg_rating=rnd(po[3], 1),
            quotations=int(po[4] or 0),
            technically_accepted=int(po[5] or 0),
            technically_rejected=int(po[6] or 0),
            notes=notes[:20],
        )

    async def history_counts(self, vendor_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
        """vendor -> (organization projects it was used on, equipment lines) for many vendors at once."""
        if not vendor_ids or not self.principal.has(P.EQUIPMENT_READ):
            return {}
        out: dict[uuid.UUID, tuple[int, int]] = {}
        for col in (ProjectEquipment.vendor_id, ProjectEquipment.manufacturer_id):
            rows = await self.s.execute(
                select(col, func.count(func.distinct(ProjectEquipment.project_id)), func.count())
                .where(col.in_(vendor_ids), ProjectEquipment.verification != "archived")
                .group_by(col)
            )
            for vid, projects, lines in rows.all():
                if vid is None:
                    continue
                prev = out.get(vid, (0, 0))
                out[vid] = (max(prev[0], int(projects)), prev[1] + int(lines))
        return out

    # ------------------------------------------------------------------ attribute definitions

    async def attributes(self) -> list[AttributeRead]:
        cats = await self._categories()
        by_id = {cid: code for code, (cid, _) in cats.items()}
        return [
            AttributeRead(
                id=a.id,
                key=a.key,
                label=a.label,
                data_type=a.data_type,
                unit=a.unit,
                quantity=a.quantity,
                synonyms=a.synonyms,
                category_code=by_id.get(a.category_id) if a.category_id else None,
                shared=a.tenant_id is None,
                sort_order=a.sort_order,
            )
            for a in await attribute_defs(self.s)
        ]

    async def create_attribute(self, body: AttributeWrite) -> AttributeRead:
        self._need(P.EQUIPMENT_MANAGE)
        if any(a.key == body.key for a in await self.attributes()):
            raise Conflict(f"An attribute with key {body.key!r} already exists", code="duplicate")
        a = EquipmentAttribute(
            tenant_id=self.tenant_id,
            category_id=await self._category_id(body.category_code),
            key=body.key,
            label=body.label,
            data_type="number" if body.quantity else "text",
            unit=canonical_unit(body.quantity),
            quantity=body.quantity,
            synonyms=[s.strip() for s in body.synonyms if s.strip()],
            sort_order=body.sort_order,
        )
        self.s.add(a)
        await self.s.flush()
        record_audit(
            self.s,
            "equipment_attribute.created",
            principal=self.principal,
            entity_type="equipment_attribute",
            entity_id=a.id,
            changes={"key": a.key},
        )
        await self.s.commit()
        return next(x for x in await self.attributes() if x.id == a.id)

    async def delete_attribute(self, attribute_id: uuid.UUID) -> None:
        self._need(P.EQUIPMENT_MANAGE)
        a = await self.s.get(EquipmentAttribute, attribute_id)
        if a is None:
            raise NotFound("Attribute not found")
        if a.tenant_id is None:
            raise Forbidden("Shared default attributes can't be removed")
        record_audit(
            self.s,
            "equipment_attribute.deleted",
            principal=self.principal,
            entity_type="equipment_attribute",
            entity_id=a.id,
            changes={"key": a.key},
        )
        await self.s.delete(a)
        await self.s.commit()

    # ------------------------------------------------------------------ MEL import

    async def upload_mel(
        self, project_id: uuid.UUID, filename: str, content_type: str | None, data: bytes, max_rows: int
    ) -> MelUploadRead:
        self._need(P.EQUIPMENT_MANAGE)
        project = await self._project(project_id)
        filename = (filename or "mel").replace("\\", "/").rsplit("/", 1)[-1][:255]
        if extension(filename) not in TABLE_EXTENSIONS:
            raise ValidationFailed(
                "Upload the MEL as Excel (.xlsx), CSV or a Word document with a table (.docx)"
            )
        if not data:
            raise ValidationFailed("The file is empty")
        book = await anyio.to_thread.run_sync(lambda: read_mel(data, filename, max_rows + 50))
        defs = await attribute_defs(self.s)
        sheets = sheet_infos(book, defs, max_rows)
        if not any(s["data_rows"] for s in sheets):
            raise ValidationFailed("The file has no data rows")
        upload = Upload(
            tenant_id=self.tenant_id,
            kind=UploadKind.SPREADSHEET,
            filename=filename,
            content_type=(content_type or "application/octet-stream")[:120],
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            meta={"mel": True, "project_id": str(project.id), "sheets": sheets},
            data=data,
        )
        self.s.add(upload)
        await self.s.flush()
        record_audit(
            self.s,
            "mel.uploaded",
            principal=self.principal,
            entity_type="upload",
            entity_id=upload.id,
            changes={"filename": filename, "project_id": str(project.id)},
        )
        await self.s.commit()
        return MelUploadRead(
            upload_id=upload.id,
            project_id=project.id,
            filename=filename,
            sheets=[MelSheet(**s) for s in sheets],
            fields=field_labels(defs),
        )

    async def _refresh_line(
        self,
        e: ProjectEquipment,
        p: dict[str, Any],
        r: MelRow,
        vendor_id: uuid.UUID | None,
        maker_id: uuid.UUID | None,
    ) -> None:
        """A re-imported MEL row (SOW 8.9): the newer revision's values replace the line's, nothing is lost that the
        new row leaves blank, and the line points at the new source."""
        before = {"source_ref": e.source_ref, "version": e.version}
        if p.get("category_code"):
            e.category_id = await self._category_id(p["category_code"])
        for field in ("name", "service", "model", "tag"):
            if p.get(field):
                setattr(e, field, p[field])
        if p.get("vendor_name"):
            e.vendor_id, e.vendor_name = vendor_id, p["vendor_name"]
        if p.get("manufacturer_name"):
            e.manufacturer_id, e.manufacturer_name = maker_id, p["manufacturer_name"]
        e.quantity = p.get("quantity") or e.quantity
        e.attributes = {**(e.attributes or {}), **(p.get("attributes") or {})}
        e.standards = list(dict.fromkeys([*(p.get("standards") or []), *e.standards]))[:20]
        e.specs_text = p.get("specs_text") or e.specs_text
        e.source_upload_id, e.source_ref = r.upload_id, (p.get("source_ref") or "")[:200]
        e.version += 1
        record_audit(
            self.s,
            "equipment.updated_from_mel",
            principal=self.principal,
            entity_type="project_equipment",
            entity_id=e.id,
            changes={"before": before, "source_ref": e.source_ref},
        )

    async def mel_uploads(self, project_id: uuid.UUID) -> list[MelUploadSummary]:
        """A project's MELs, newest first, so a review can be picked up from any browser."""
        self._need(P.EQUIPMENT_READ)
        project = await self._project(project_id)
        uploads = list(
            await self.s.scalars(
                select(Upload)
                .where(
                    Upload.meta["mel"].as_boolean().is_(True),
                    Upload.meta["project_id"].astext == str(project.id),
                )
                .order_by(Upload.created_at.desc())
                .limit(100)
            )
        )
        if not uploads:
            return []
        ids = [u.id for u in uploads]
        counts: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
        for upload_id, row_status, n in await self.s.execute(
            select(MelRow.upload_id, MelRow.status, func.count())
            .where(MelRow.upload_id.in_(ids))
            .group_by(MelRow.upload_id, MelRow.status)
        ):
            counts[upload_id][row_status] = int(n)
        jobs: dict[str, Job] = {}
        for job in await self.s.scalars(
            select(Job)
            .where(Job.type == JobType.MEL_IMPORT, Job.input["upload_id"].astext.in_([str(i) for i in ids]))
            .order_by(Job.created_at)
        ):
            jobs[job.input["upload_id"]] = job  # the latest parse wins
        return [
            MelUploadSummary(
                upload_id=u.id,
                filename=u.filename,
                uploaded_at=u.created_at,
                job_id=(latest := jobs.get(str(u.id))) and latest.id,
                job_status=latest.status.value if latest else None,
                counts=counts.get(u.id, {}),
            )
            for u in uploads
        ]

    async def mel_upload(self, upload_id: uuid.UUID) -> Upload:
        upload = await self.s.get(Upload, upload_id)
        if upload is None or not (upload.meta or {}).get("mel"):
            raise NotFound("MEL upload not found")
        return upload

    async def mel_rows(self, upload_id: uuid.UUID, status: str | None) -> MelRows:
        self._need(P.EQUIPMENT_READ)
        upload = await self.mel_upload(upload_id)
        counts = dict(
            (
                await self.s.execute(
                    select(MelRow.status, func.count())
                    .where(MelRow.upload_id == upload_id)
                    .group_by(MelRow.status)
                )
            ).all()
        )
        stmt = select(MelRow).where(MelRow.upload_id == upload_id)
        if status:
            stmt = stmt.where(MelRow.status == status)
        rows = await self.s.scalars(stmt.order_by(MelRow.row_number).limit(5000))
        # Prices (PO value, quoted price) are commercial data: hidden without commercial:read, as elsewhere.
        commercial = self.principal.has(P.COMMERCIAL_READ)

        def raw(r: MelRow) -> dict[str, Any]:
            return (
                r.raw
                if commercial
                else {k: v for k, v in (r.raw or {}).items() if not COMMERCIAL_HEADER.search(k)}
            )

        def parsed(r: MelRow) -> dict[str, Any]:
            if commercial:
                return r.parsed
            return {k: v for k, v in (r.parsed or {}).items() if k not in {"amount", "currency"}}

        return MelRows(
            upload_id=upload_id,
            project_id=uuid.UUID(upload.meta["project_id"]),
            filename=upload.filename,
            counts={k: int(v) for k, v in counts.items()},
            rows=[
                MelRowRead(
                    id=r.id,
                    row_number=r.row_number,
                    raw=raw(r),
                    parsed=parsed(r),
                    issues=r.issues,
                    status=r.status,
                    equipment_id=r.equipment_id,
                )
                for r in rows
            ],
        )

    async def update_mel_row(self, row_id: uuid.UUID, body: MelRowUpdate) -> MelRowRead:
        self._need(P.EQUIPMENT_MANAGE)
        r = await self.s.get(MelRow, row_id)
        if r is None:
            raise NotFound("Row not found")
        if r.status != "pending":
            raise Conflict("This row was already decided")
        parsed = dict(r.parsed)
        changes = body.model_dump(exclude_unset=True, mode="json")
        if "category_code" in changes:
            cats = await self._categories()
            if changes["category_code"] and changes["category_code"] not in cats:
                raise ValidationFailed("Unknown equipment category")
            parsed["category_name"] = cats[changes["category_code"]][1] if changes["category_code"] else None
        for role in ("vendor", "manufacturer"):
            if changes.get(f"{role}_id"):
                v = await self.s.get(Vendor, uuid.UUID(changes[f"{role}_id"]))
                if v is None:
                    raise NotFound(f"{role.capitalize()} not found")
                parsed[f"{role}_name"], parsed[f"{role}_match"] = v.display_name, "manual"
        parsed.update(changes)
        r.parsed = parsed
        r.issues = [
            i
            for i in r.issues
            if not (
                ("category_code" in changes and i.startswith("Equipment type"))
                or any(
                    changes.get(f"{role}_id") and i.startswith(role.capitalize())
                    for role in ("vendor", "manufacturer")
                )
                or ("name" in changes and i == "No description")
            )
        ]
        await self.s.commit()
        return MelRowRead(
            id=r.id,
            row_number=r.row_number,
            raw=r.raw,
            parsed=r.parsed,
            issues=r.issues,
            status=r.status,
            equipment_id=r.equipment_id,
        )

    async def decide_mel(self, body: MelDecision) -> MelDecisionResult:
        self._need(P.EQUIPMENT_MANAGE)
        rows = list(await self.s.scalars(select(MelRow).where(MelRow.id.in_(body.row_ids))))
        result = MelDecisionResult(
            approved=0, rejected=0, skipped=len(body.row_ids) - len(rows), vendors_linked=0
        )
        linked: set[uuid.UUID] = set(await self.s.scalars(select(TenantVendorLink.vendor_id)))
        for r in rows:
            if r.status != "pending":
                result.skipped += 1
                continue
            if body.decision == "reject":
                r.status = "rejected"
                result.rejected += 1
                continue
            p = r.parsed
            if not p.get("name"):
                result.skipped += 1  # nothing to record without a description
                continue
            vendor_id = uuid.UUID(p["vendor_id"]) if p.get("vendor_id") else None
            maker_id = uuid.UUID(p["manufacturer_id"]) if p.get("manufacturer_id") else None
            existing = (
                await self.s.get(ProjectEquipment, uuid.UUID(p["duplicate_of"]))
                if p.get("duplicate_of")
                else None
            )
            if existing is not None and existing.project_id == r.project_id:
                await self._refresh_line(existing, p, r, vendor_id, maker_id)
                r.status, r.equipment_id = "approved", existing.id
                result.updated += 1
                continue
            e = ProjectEquipment(
                tenant_id=self.tenant_id,
                project_id=r.project_id,
                category_id=await self._category_id(p.get("category_code")),
                vendor_id=vendor_id,
                vendor_name=p.get("vendor_name") or "",
                manufacturer_id=maker_id,
                manufacturer_name=p.get("manufacturer_name") or "",
                tag=p.get("tag") or "",
                name=p["name"],
                service=p.get("service") or "",
                model=p.get("model") or "",
                quantity=p.get("quantity") or 1,
                attributes=p.get("attributes") or {},
                standards=p.get("standards") or [],
                specs_text=p.get("specs_text") or "",
                department=p.get("department"),
                verification=body.verification,
                origin="mel",
                source_upload_id=r.upload_id,
                source_ref=(p.get("source_ref") or "")[:200],
            )
            self.s.add(e)
            await self.s.flush()
            if p.get("po_number") or p.get("amount") or p.get("technical_status") or p.get("lead_time_weeks"):
                # A PO when it has a number; otherwise a quotation / bid evaluation line.
                self.s.add(
                    ProcurementRecord(
                        tenant_id=self.tenant_id,
                        project_id=r.project_id,
                        equipment_id=e.id,
                        vendor_id=vendor_id,
                        vendor_name=p.get("vendor_name") or "",
                        po_number=p.get("po_number") or "",
                        record_type="po" if p.get("po_number") else "quotation",
                        technical_status=p.get("technical_status"),
                        amount=p.get("amount"),
                        currency=p.get("currency"),
                        lead_time_weeks=p.get("lead_time_weeks"),
                        verification=body.verification,
                    )
                )
            for vid in (vendor_id, maker_id):
                if vid and vid not in linked:
                    self.s.add(
                        TenantVendorLink(
                            tenant_id=self.tenant_id, vendor_id=vid, avl_status=AvlStatus.UNASSESSED
                        )
                    )
                    linked.add(vid)
                    result.vendors_linked += 1
            r.status, r.equipment_id = "approved", e.id
            result.approved += 1
        record_audit(
            self.s,
            "mel.reviewed",
            principal=self.principal,
            entity_type="upload",
            entity_id=rows[0].upload_id if rows else None,
            changes={"approved": result.approved, "rejected": result.rejected},
        )
        await self.s.commit()
        return result

    # ------------------------------------------------------------------ saved searches

    async def saved_searches(self, kind: str | None) -> list[SavedSearch]:
        stmt = select(SavedSearch).where(SavedSearch.user_id == self.principal.user_id)
        if kind:
            stmt = stmt.where(SavedSearch.kind == kind)
        return list(await self.s.scalars(stmt.order_by(SavedSearch.name)))

    async def save_search(self, body: SavedSearchWrite) -> SavedSearch:
        if self.principal.user_id is None:
            raise Forbidden("Saved searches belong to a user")
        row = SavedSearch(tenant_id=self._tenant, user_id=self.principal.user_id, **body.model_dump())
        self.s.add(row)
        await self.s.commit()
        await self.s.refresh(row)
        return row

    async def delete_saved_search(self, search_id: uuid.UUID) -> None:
        row = await self.s.get(SavedSearch, search_id)
        if row is None or row.user_id != self.principal.user_id:
            raise NotFound("Saved search not found")
        await self.s.delete(row)
        await self.s.commit()

    # ------------------------------------------------------------------ category browser

    async def category_tree(self) -> list[CategoryNode]:
        cats = list(
            await self.s.scalars(
                select(EquipmentCategory).order_by(EquipmentCategory.sort_order, EquipmentCategory.name)
            )
        )
        vendors: dict[str, set[uuid.UUID]] = defaultdict(set)
        for code, vid in (
            await self.s.execute(
                select(EquipmentCategory.code, VendorEquipment.vendor_id)
                .join(VendorEquipment, VendorEquipment.category_id == EquipmentCategory.id)
                .where(VendorEquipment.status == "approved")
            )
        ).all():
            vendors[code].add(vid)
        equipment: dict[str, int] = {}
        if self._tenant is not None and self.principal.has(P.EQUIPMENT_READ):
            equipment = {
                code: int(n)
                for code, n in (
                    await self.s.execute(
                        select(EquipmentCategory.code, func.count())
                        .join(ProjectEquipment, ProjectEquipment.category_id == EquipmentCategory.id)
                        .where(ProjectEquipment.verification != "archived")
                        .group_by(EquipmentCategory.code)
                    )
                ).all()
            }
        nodes = {
            c.code: CategoryNode(
                code=c.code,
                name=c.name,
                level=c.level,
                standards=c.standards,
                vendor_count=0,
                equipment_count=0,
            )
            for c in cats
        }
        by_parent: dict[uuid.UUID | None, list[EquipmentCategory]] = defaultdict(list)
        for c in cats:
            by_parent[c.parent_id].append(c)

        def build(c: EquipmentCategory) -> tuple[CategoryNode, set[uuid.UUID], int]:
            node = nodes[c.code]
            seen, count = set(vendors.get(c.code, set())), equipment.get(c.code, 0)
            for child in by_parent.get(c.id, []):
                child_node, child_vendors, child_count = build(child)
                node.children.append(child_node)
                seen |= child_vendors
                count += child_count
            node.vendor_count, node.equipment_count = len(seen), count
            return node, seen, count

        return [build(c)[0] for c in by_parent.get(None, [])]


_SERVICE_STOP = {"and", "the", "for", "of", "service", "duty", "system", "water"}


def _service_overlap(wanted: str, have: str) -> float:
    """Share of the requested service's words found in the line's service ("sea water" ~ "seawater lift")."""

    def words(text: str) -> set[str]:
        text = re.sub(r"sea\s*water|\bs\.?w\b", "seawater", text.lower())
        text = re.sub(r"fresh\s*water|\bf\.?w\b", "freshwater", text)
        flat = re.sub(r"[^a-z0-9]+", " ", text)
        return {w.rstrip("s") for w in flat.split() if len(w) > 2 and w not in _SERVICE_STOP}

    want = words(wanted)
    if not want:
        return 1.0
    return len(want & words(have)) / len(want)


_MATERIALS = (
    (r"\bduplex\b|\bdss\b|22\s*cr", "duplex"),
    (r"super\s*duplex|\bsdss\b|25\s*cr", "superduplex"),
    (r"\b(?:ss|aisi)?\s*316l?\b|stainless", "ss316"),
    (r"\bltcs\b|low\s*temp\w*\s*carbon", "ltcs"),
    (r"\bcs\b|carbon\s*steel", "cs"),
    (r"titanium|\bti\b", "titanium"),
    (r"inconel|alloy\s*625|alloy\s*825|nickel", "nickel"),
    (r"bronze|nab|aluminium\s*bronze", "bronze"),
    (r"\bgrp\b|\bfrp\b|glass\s*reinforced", "grp"),
)


def _text_overlap(wanted: str, have: str) -> float:
    """Share of the requested words (and material families: "DSS 22Cr" = duplex) found in the line's text."""

    def words(text: str) -> set[str]:
        low = text.lower()
        families = {tag for pattern, tag in _MATERIALS if re.search(pattern, low)}
        flat = re.sub(r"[^a-z0-9]+", " ", low)
        return {w for w in flat.split() if len(w) > 2} | families

    want = words(wanted)
    if not want:
        return 1.0
    return len(want & words(have)) / len(want)
