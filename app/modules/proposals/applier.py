"""Applies approved proposal items to the canonical knowledge-base tables.

Rules:
- Runs inside the caller's transaction: a proposal decision either fully applies or not at all.
- Idempotent per fact: approving "the same pump" twice links to the existing row instead of duplicating it.
- Taxonomy codes are resolved here, so a code removed after submission fails loudly instead of being guessed.
- A website already owned by another vendor is a conflict (merge instead), never silently re-assigned.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, ValidationFailed
from app.domain.enums import LocationType, ProposalItemAction, SourceKind, VendorStatus
from app.domain.normalization import normalize_name, registrable_domain
from app.modules.proposals.models import ProposalItem
from app.modules.taxonomy.models import CertificationType, EquipmentCategory, Industry, ServiceCategory
from app.modules.vendors.models import (
    FactSource,
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


@dataclass(frozen=True)
class Applied:
    table: str
    entity_id: uuid.UUID
    vendor_id: uuid.UUID


class Applier:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ------------------------------------------------------------------ helpers

    async def _code(self, model: Any, code: str, label: str) -> Any:
        row = await self.s.scalar(select(model).where(model.code == code))
        if row is None:
            raise ValidationFailed(
                f"Unknown {label} {code!r}",
                errors=[{"field": "payload", "message": f"unknown {label}: {code}"}],
            )
        return row

    async def _claim_domain(self, vendor_id: uuid.UUID, domain: str, *, primary: bool) -> VendorDomain:
        existing = await self.s.scalar(select(VendorDomain).where(VendorDomain.domain == domain))
        if existing is not None:
            if existing.vendor_id != vendor_id:
                raise Conflict(
                    f"The website {domain} already belongs to another vendor ({existing.vendor_id}). "
                    "Reject this item, or merge the two vendors.",
                    code="duplicate_vendor",
                )
            return existing
        row = VendorDomain(vendor_id=vendor_id, domain=domain, is_primary=primary)
        self.s.add(row)
        await self.s.flush()
        return row

    async def attach_evidence(
        self, fact_table: str, fact_id: uuid.UUID, vendor_id: uuid.UUID, evidence: list[dict[str, Any]]
    ) -> None:
        for ev in evidence:
            url = ev.get("url")
            title = ev.get("title") or (url or "")
            source = await self.s.scalar(
                select(VendorSource).where(
                    VendorSource.vendor_id == vendor_id,
                    VendorSource.url.is_(None) if url is None else VendorSource.url == url,
                    VendorSource.title == title,
                )
            )
            if source is None:
                source = VendorSource(
                    vendor_id=vendor_id,
                    kind=SourceKind(ev.get("kind", SourceKind.MANUAL)),
                    url=url,
                    title=title[:300],
                    retrieved_at=datetime.now(UTC),
                )
                self.s.add(source)
                await self.s.flush()
            self.s.add(
                FactSource(
                    fact_table=fact_table,
                    fact_id=fact_id,
                    source_id=source.id,
                    snippet=ev.get("snippet", ""),
                    page=ev.get("page"),
                )
            )

    # ------------------------------------------------------------------ dispatch

    async def apply(self, item: ProposalItem, vendor_id: uuid.UUID | None) -> Applied:
        p = item.payload
        action = item.action
        if action == ProposalItemAction.CREATE_VENDOR:
            result = await self._create_vendor(p)
        else:
            if vendor_id is None:
                raise Conflict("This item needs its vendor to be created first", code="vendor_missing")
            vendor = await self.s.get(Vendor, vendor_id, with_for_update=True)
            if vendor is None or vendor.status == VendorStatus.RETIRED:
                raise Conflict("The vendor no longer exists", code="vendor_missing")
            handler = {
                ProposalItemAction.UPDATE_VENDOR_FIELD: self._update_field,
                ProposalItemAction.ADD_DOMAIN: self._add_domain,
                ProposalItemAction.ADD_ALIAS: self._add_alias,
                ProposalItemAction.ADD_LOCATION: self._add_location,
                ProposalItemAction.ADD_INDUSTRY: self._add_industry,
                ProposalItemAction.ADD_EQUIPMENT: self._add_equipment,
                ProposalItemAction.ADD_SERVICE: self._add_service,
                ProposalItemAction.ADD_CERTIFICATION: self._add_certification,
                ProposalItemAction.ADD_PROJECT: self._add_project,
            }[action]
            result = await handler(vendor, p)
            vendor.knowledge_version += 1
        if item.evidence:
            await self.attach_evidence(result.table, result.entity_id, result.vendor_id, item.evidence)
        return result

    # ------------------------------------------------------------------ actions

    async def _create_vendor(self, p: dict[str, Any]) -> Applied:
        domain = registrable_domain(p.get("website"))
        if domain:
            existing = await self.s.scalar(select(VendorDomain).where(VendorDomain.domain == domain))
            if existing is not None:
                raise Conflict(
                    f"A vendor with the website {domain} already exists ({existing.vendor_id}). "
                    "Reject this proposal or add its facts to that vendor instead.",
                    code="duplicate_vendor",
                )
        vendor = Vendor(
            legal_name=p["legal_name"],
            display_name=p.get("display_name") or p["legal_name"],
            normalized_name=normalize_name(p["legal_name"]),
            website=p.get("website"),
            hq_country=p.get("hq_country"),
            hq_city=p.get("hq_city"),
            description=p.get("description") or "",
            status=VendorStatus.ACTIVE,
            knowledge_version=1,
        )
        self.s.add(vendor)
        await self.s.flush()
        if domain:
            await self._claim_domain(vendor.id, domain, primary=True)
        if p.get("hq_country") or p.get("hq_city"):
            self.s.add(
                VendorLocation(
                    vendor_id=vendor.id,
                    location_type=LocationType.HQ,
                    city=p.get("hq_city"),
                    country=p.get("hq_country"),
                )
            )
        return Applied("vendors", vendor.id, vendor.id)

    async def _update_field(self, vendor: Vendor, p: dict[str, Any]) -> Applied:
        field, value = p["field"], p.get("value")
        setattr(vendor, field, value if value is not None else ("" if field == "description" else None))
        if field == "legal_name":
            vendor.normalized_name = normalize_name(value or "")
        if field == "website" and value:
            domain = registrable_domain(value)
            if domain:
                await self._claim_domain(vendor.id, domain, primary=False)
        return Applied("vendors", vendor.id, vendor.id)

    async def _add_domain(self, vendor: Vendor, p: dict[str, Any]) -> Applied:
        row = await self._claim_domain(vendor.id, p["domain"], primary=False)
        return Applied("vendor_domains", row.id, vendor.id)

    async def _add_alias(self, vendor: Vendor, p: dict[str, Any]) -> Applied:
        norm = normalize_name(p["alias"])
        row = await self.s.scalar(
            select(VendorAlias).where(
                VendorAlias.vendor_id == vendor.id, VendorAlias.normalized_alias == norm
            )
        )
        if row is None:
            row = VendorAlias(
                vendor_id=vendor.id, alias=p["alias"], normalized_alias=norm, alias_type=p["alias_type"]
            )
            self.s.add(row)
            await self.s.flush()
        return Applied("vendor_aliases", row.id, vendor.id)

    async def _add_location(self, vendor: Vendor, p: dict[str, Any]) -> Applied:
        row = VendorLocation(
            vendor_id=vendor.id,
            location_type=LocationType(p["location_type"]),
            address=p.get("address") or "",
            city=p.get("city"),
            country=p.get("country"),
        )
        self.s.add(row)
        await self.s.flush()
        return Applied("vendor_locations", row.id, vendor.id)

    async def _add_industry(self, vendor: Vendor, p: dict[str, Any]) -> Applied:
        industry = await self._code(Industry, p["industry_code"], "industry")
        exists = await self.s.get(VendorIndustry, (vendor.id, industry.id))
        if exists is None:
            self.s.add(VendorIndustry(vendor_id=vendor.id, industry_id=industry.id))
            await self.s.flush()
        return Applied("vendor_industries", industry.id, vendor.id)

    async def _add_equipment(self, vendor: Vendor, p: dict[str, Any]) -> Applied:
        category = await self._code(EquipmentCategory, p["category_code"], "equipment category")
        key = (p.get("model_series") or p["name"]).lower()
        row = await self.s.scalar(
            select(VendorEquipment).where(
                VendorEquipment.vendor_id == vendor.id,
                VendorEquipment.category_id == category.id,
                func.lower(func.coalesce(VendorEquipment.model_series, VendorEquipment.name)) == key,
            )
        )
        if row is None:
            row = VendorEquipment(
                vendor_id=vendor.id,
                category_id=category.id,
                name=p["name"],
                model_series=p.get("model_series"),
                standards=p.get("standards") or [],
                specs=p.get("specs") or {},
                applications=p.get("applications") or [],
            )
            self.s.add(row)
            await self.s.flush()
        return Applied("vendor_equipment", row.id, vendor.id)

    async def _add_service(self, vendor: Vendor, p: dict[str, Any]) -> Applied:
        category = await self._code(ServiceCategory, p["category_code"], "service category")
        eq_category = (
            await self._code(EquipmentCategory, p["equipment_category_code"], "equipment category")
            if p.get("equipment_category_code")
            else None
        )
        row = await self.s.scalar(
            select(VendorService).where(
                VendorService.vendor_id == vendor.id,
                VendorService.category_id == category.id,
                func.lower(VendorService.name) == p["name"].lower(),
            )
        )
        if row is None:
            row = VendorService(
                vendor_id=vendor.id,
                category_id=category.id,
                equipment_category_id=eq_category.id if eq_category else None,
                name=p["name"],
                regions=p.get("regions") or [],
                details={},
            )
            self.s.add(row)
            await self.s.flush()
        return Applied("vendor_services", row.id, vendor.id)

    async def _add_certification(self, vendor: Vendor, p: dict[str, Any]) -> Applied:
        cert_type = await self._code(CertificationType, p["certification_code"], "certification")
        number = p.get("certificate_number")
        row = await self.s.scalar(
            select(VendorCertification).where(
                VendorCertification.vendor_id == vendor.id,
                VendorCertification.certification_type_id == cert_type.id,
                func.coalesce(VendorCertification.certificate_number, "") == (number or ""),
            )
        )
        if row is None:
            row = VendorCertification(
                vendor_id=vendor.id,
                certification_type_id=cert_type.id,
                certificate_number=number,
                scope=p.get("scope") or "",
                # Payloads are stored as JSON, so dates arrive as ISO strings.
                valid_from=date.fromisoformat(p["valid_from"]) if p.get("valid_from") else None,
                valid_to=date.fromisoformat(p["valid_to"]) if p.get("valid_to") else None,
            )
            self.s.add(row)
            await self.s.flush()
        return Applied("vendor_certifications", row.id, vendor.id)

    async def _add_project(self, vendor: Vendor, p: dict[str, Any]) -> Applied:
        row = await self.s.scalar(
            select(VendorProject).where(
                VendorProject.vendor_id == vendor.id,
                func.lower(VendorProject.project_name) == p["project_name"].lower(),
                VendorProject.year.is_not_distinct_from(p.get("year")),
            )
        )
        if row is None:
            row = VendorProject(
                vendor_id=vendor.id,
                project_name=p["project_name"],
                client=p.get("client"),
                operator=p.get("operator"),
                facility_type=p.get("facility_type"),
                country=p.get("country"),
                year=p.get("year"),
                scope=p.get("scope") or "",
            )
            self.s.add(row)
            await self.s.flush()
        return Applied("vendor_projects", row.id, vendor.id)
