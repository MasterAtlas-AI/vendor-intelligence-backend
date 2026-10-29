"""Read side of the global knowledge base, merged with the caller's tenant overlay (AVL status, codes, tags)."""

import base64
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Forbidden, NotFound, ValidationFailed
from app.domain.enums import AvlStatus, FactStatus, VendorStatus
from app.domain.normalization import normalize_country, normalize_name
from app.domain.principal import Principal
from app.modules.audit.service import diff, record_audit
from app.modules.taxonomy.models import CertificationType, EquipmentCategory, Industry, ServiceCategory
from app.modules.tenant_vendors.models import TenantVendorLink
from app.modules.vendors.models import (
    Vendor,
    VendorAlias,
    VendorCertification,
    VendorDomain,
    VendorEquipment,
    VendorIndustry,
    VendorLocation,
    VendorProject,
    VendorService,
    VendorSource,
)
from app.modules.vendors.schemas import (
    CategoryRef,
    CertificationRead,
    EquipmentRead,
    LocationRead,
    ProjectRead,
    ServiceRead,
    SourceRead,
    TenantOverlayRead,
    TenantOverlayUpdate,
    VendorDetail,
    VendorSummary,
)

MAX_LIMIT = 100


@dataclass(frozen=True)
class VendorFilters:
    q: str | None = None
    country: str | None = None
    equipment_category: str | None = None  # taxonomy code; includes descendants
    service_category: str | None = None
    certification: str | None = None
    industry: str | None = None
    avl_status: AvlStatus | None = None
    mine: bool = False  # only vendors on the caller's own list (any AVL status)
    include_inactive: bool = False
    vendor_ids: tuple[uuid.UUID, ...] | None = None  # restrict to these (search results)


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _encode_cursor(name: str, id_: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(json.dumps([name, str(id_)]).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, uuid.UUID]:
    try:
        name, id_ = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return str(name), uuid.UUID(id_)
    except (ValueError, TypeError) as exc:
        raise ValidationFailed("Invalid cursor", errors=[{"field": "cursor", "message": "Invalid"}]) from exc


def _approved(model: Any) -> ColumnElement[bool]:
    return model.status == FactStatus.APPROVED  # type: ignore[no-any-return]


def _in_category(fact: Any, category: Any, code: str) -> ColumnElement[bool]:
    """EXISTS an approved fact of this vendor in `code` or any descendant category."""
    return exists(
        select(1)
        .select_from(fact)
        .join(category, category.id == fact.category_id)
        .where(
            fact.vendor_id == Vendor.id,
            _approved(fact),
            or_(category.code == code, category.code.like(f"{code}.%")),
        )
    )


def _count(fact: Any) -> Any:
    return (
        select(func.count())
        .select_from(fact)
        .where(fact.vendor_id == Vendor.id, _approved(fact))
        .scalar_subquery()
    )


class VendorQueryService:
    def __init__(self, session: AsyncSession, principal: Principal) -> None:
        self.s = session
        self.principal = principal

    # ------------------------------------------------------------------ search

    def _filtered(self, f: VendorFilters) -> Select[Any]:
        stmt = select(
            Vendor,
            _count(VendorEquipment).label("equipment_count"),
            _count(VendorService).label("service_count"),
            _count(VendorCertification).label("certification_count"),
        )
        if not f.include_inactive:
            stmt = stmt.where(Vendor.status == VendorStatus.ACTIVE)
        if f.q and f.q.strip():
            q = f.q.strip()
            norm = normalize_name(q)
            conditions: list[ColumnElement[bool]] = [
                Vendor.search_tsv.op("@@")(func.websearch_to_tsquery("simple", q)),
                exists(
                    select(1).where(
                        VendorDomain.vendor_id == Vendor.id, VendorDomain.domain.ilike(_like(q.lower()))
                    )
                ),
            ]
            if norm:
                conditions += [
                    Vendor.normalized_name.ilike(_like(norm)),
                    exists(
                        select(1).where(
                            VendorAlias.vendor_id == Vendor.id,
                            VendorAlias.normalized_alias.ilike(_like(norm)),
                        )
                    ),
                ]
            stmt = stmt.where(or_(*conditions))
        if f.vendor_ids is not None:
            stmt = stmt.where(Vendor.id.in_(f.vendor_ids))
        if f.country:
            country = normalize_country(f.country)
            if country is None:
                raise ValidationFailed(
                    "Unknown country", errors=[{"field": "country", "message": "Use ISO code"}]
                )
            stmt = stmt.where(
                or_(
                    Vendor.hq_country == country,
                    exists(
                        select(1).where(
                            VendorLocation.vendor_id == Vendor.id,
                            VendorLocation.country == country,
                            _approved(VendorLocation),
                        )
                    ),
                )
            )
        if f.equipment_category:
            stmt = stmt.where(_in_category(VendorEquipment, EquipmentCategory, f.equipment_category))
        if f.service_category:
            stmt = stmt.where(_in_category(VendorService, ServiceCategory, f.service_category))
        if f.certification:
            stmt = stmt.where(
                exists(
                    select(1)
                    .select_from(VendorCertification)
                    .join(
                        CertificationType, CertificationType.id == VendorCertification.certification_type_id
                    )
                    .where(
                        VendorCertification.vendor_id == Vendor.id,
                        _approved(VendorCertification),
                        CertificationType.code == f.certification,
                    )
                )
            )
        if f.industry:
            stmt = stmt.where(
                exists(
                    select(1)
                    .select_from(VendorIndustry)
                    .join(Industry, Industry.id == VendorIndustry.industry_id)
                    .where(VendorIndustry.vendor_id == Vendor.id, Industry.code == f.industry)
                )
            )
        if f.avl_status or f.mine:
            tenant_id = self._require_tenant()
            on_list = select(1).where(
                TenantVendorLink.vendor_id == Vendor.id, TenantVendorLink.tenant_id == tenant_id
            )
            if f.avl_status:
                on_list = on_list.where(TenantVendorLink.avl_status == f.avl_status)
            stmt = stmt.where(exists(on_list))
        return stmt

    async def search(
        self, f: VendorFilters, *, limit: int, cursor: str | None
    ) -> tuple[list[VendorSummary], str | None]:
        limit = max(1, min(limit, MAX_LIMIT))
        sort_key = func.lower(Vendor.display_name)
        stmt = self._filtered(f)
        if cursor:
            name, last_id = _decode_cursor(cursor)
            stmt = stmt.where(tuple_(sort_key, Vendor.id) > tuple_(name, last_id))
        rows = (await self.s.execute(stmt.order_by(sort_key, Vendor.id).limit(limit + 1))).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        overlays = await self._overlays([r[0].id for r in rows])
        items = [
            VendorSummary(
                id=v.id,
                display_name=v.display_name,
                legal_name=v.legal_name,
                website=v.website,
                hq_country=v.hq_country,
                hq_city=v.hq_city,
                status=v.status,
                equipment_count=eq,
                service_count=sv,
                certification_count=ct,
                tenant=overlays.get(v.id),
            )
            for v, eq, sv, ct in rows
        ]
        nxt = _encode_cursor(rows[-1][0].display_name.lower(), rows[-1][0].id) if has_more else None
        return items, nxt

    async def _overlays(self, vendor_ids: list[uuid.UUID]) -> dict[uuid.UUID, TenantOverlayRead]:
        if not vendor_ids or self.principal.tenant_id is None:
            return {}
        links = await self.s.scalars(
            select(TenantVendorLink).where(
                TenantVendorLink.tenant_id == self.principal.tenant_id,
                TenantVendorLink.vendor_id.in_(vendor_ids),
            )
        )
        return {link.vendor_id: TenantOverlayRead.model_validate(link) for link in links}

    # ------------------------------------------------------------------ detail

    async def detail(self, vendor_id: uuid.UUID) -> VendorDetail:
        row = (
            await self.s.execute(
                self._filtered(VendorFilters(include_inactive=True)).where(Vendor.id == vendor_id)
            )
        ).first()
        if row is None:
            raise NotFound("Vendor not found")
        v, eq_count, sv_count, ct_count = row

        equipment = (
            await self.s.execute(
                select(VendorEquipment, EquipmentCategory)
                .join(EquipmentCategory, EquipmentCategory.id == VendorEquipment.category_id)
                .where(VendorEquipment.vendor_id == v.id, _approved(VendorEquipment))
                .order_by(EquipmentCategory.code, VendorEquipment.name)
            )
        ).all()
        eq_cat = EquipmentCategory.__table__.alias("service_equipment_category")
        services = (
            await self.s.execute(
                select(VendorService, ServiceCategory, eq_cat.c.id, eq_cat.c.code, eq_cat.c.name)
                .join(ServiceCategory, ServiceCategory.id == VendorService.category_id)
                .outerjoin(eq_cat, eq_cat.c.id == VendorService.equipment_category_id)
                .where(VendorService.vendor_id == v.id, _approved(VendorService))
                .order_by(ServiceCategory.code, VendorService.name)
            )
        ).all()
        certs = (
            await self.s.execute(
                select(VendorCertification, CertificationType)
                .join(CertificationType, CertificationType.id == VendorCertification.certification_type_id)
                .where(VendorCertification.vendor_id == v.id, _approved(VendorCertification))
                .order_by(CertificationType.name)
            )
        ).all()
        industries = await self.s.scalars(
            select(Industry)
            .join(VendorIndustry, VendorIndustry.industry_id == Industry.id)
            .where(VendorIndustry.vendor_id == v.id)
            .order_by(Industry.name)
        )
        locations = await self.s.scalars(
            select(VendorLocation).where(VendorLocation.vendor_id == v.id, _approved(VendorLocation))
        )
        projects = await self.s.scalars(
            select(VendorProject)
            .where(VendorProject.vendor_id == v.id, _approved(VendorProject))
            .order_by(VendorProject.year.desc().nulls_last(), VendorProject.project_name)
        )
        sources = await self.s.scalars(
            select(VendorSource)
            .where(VendorSource.vendor_id == v.id)
            .order_by(VendorSource.created_at.desc())
        )
        overlays = await self._overlays([v.id])

        def ref(model: Any) -> CategoryRef:
            return CategoryRef(id=model.id, code=model.code, name=model.name)

        return VendorDetail(
            id=v.id,
            display_name=v.display_name,
            legal_name=v.legal_name,
            website=v.website,
            hq_country=v.hq_country,
            hq_city=v.hq_city,
            status=v.status,
            equipment_count=eq_count,
            service_count=sv_count,
            certification_count=ct_count,
            tenant=overlays.get(v.id),
            description=v.description,
            domains=[d.domain for d in v.domains],
            aliases=[a.alias for a in v.aliases],
            industries=[ref(i) for i in industries],
            data_quality_score=v.data_quality_score,
            last_verified_at=v.last_verified_at,
            knowledge_version=v.knowledge_version,
            locations=[LocationRead.model_validate(x) for x in locations],
            equipment=[
                EquipmentRead(
                    id=e.id,
                    category=ref(c),
                    name=e.name,
                    model_series=e.model_series,
                    standards=e.standards,
                    specs=e.specs,
                    applications=e.applications,
                )
                for e, c in equipment
            ],
            services=[
                ServiceRead(
                    id=s.id,
                    category=ref(c),
                    equipment_category=CategoryRef(id=ec_id, code=ec_code, name=ec_name) if ec_id else None,
                    name=s.name,
                    regions=s.regions,
                )
                for s, c, ec_id, ec_code, ec_name in services
            ],
            certifications=[
                CertificationRead(
                    id=c.id,
                    certification=ref(t),
                    certificate_number=c.certificate_number,
                    scope=c.scope,
                    valid_from=c.valid_from,
                    valid_to=c.valid_to,
                    verified=c.verified,
                )
                for c, t in certs
            ],
            projects=[ProjectRead.model_validate(p) for p in projects],
            sources=[SourceRead.model_validate(s) for s in sources],
        )

    # ------------------------------------------------------------------ tenant overlay ("My vendors")

    def _require_tenant(self) -> uuid.UUID:
        if self.principal.tenant_id is None:
            raise Forbidden(
                "Select a tenant (X-Act-As-Tenant) to use its vendor list", code="tenant_required"
            )
        return self.principal.tenant_id

    async def set_overlay(self, vendor_id: uuid.UUID, body: TenantOverlayUpdate) -> TenantOverlayRead:
        tenant_id = self._require_tenant()
        vendor = await self.s.get(Vendor, vendor_id)
        if vendor is None or vendor.status == VendorStatus.RETIRED:
            raise NotFound("Vendor not found")
        link = await self.s.scalar(
            select(TenantVendorLink).where(
                and_(TenantVendorLink.tenant_id == tenant_id, TenantVendorLink.vendor_id == vendor_id)
            )
        )
        created = link is None
        if link is None:
            link = TenantVendorLink(
                tenant_id=tenant_id, vendor_id=vendor_id, tags=[], notes="", custom_fields={}
            )
            self.s.add(link)
        values = body.model_dump(exclude_unset=True)
        if "tags" in values and values["tags"] is not None:
            values["tags"] = sorted({t.strip() for t in values["tags"] if t.strip()})
        before = {k: getattr(link, k, None) for k in values}
        for key, value in values.items():
            if value is not None or key == "internal_vendor_code":
                setattr(link, key, value)
        await self.s.flush()
        record_audit(
            self.s,
            "tenant_vendor.created" if created else "tenant_vendor.updated",
            principal=self.principal,
            entity_type="vendor",
            entity_id=vendor_id,
            changes=diff(before, {k: getattr(link, k) for k in values}),
        )
        await self.s.commit()
        await self.s.refresh(link)
        return TenantOverlayRead.model_validate(link)

    async def remove_overlay(self, vendor_id: uuid.UUID) -> None:
        tenant_id = self._require_tenant()
        link = await self.s.scalar(
            select(TenantVendorLink).where(
                TenantVendorLink.tenant_id == tenant_id, TenantVendorLink.vendor_id == vendor_id
            )
        )
        if link is None:
            raise NotFound("This vendor isn't in your list")
        await self.s.delete(link)
        record_audit(
            self.s,
            "tenant_vendor.removed",
            principal=self.principal,
            entity_type="vendor",
            entity_id=vendor_id,
        )
        await self.s.commit()
