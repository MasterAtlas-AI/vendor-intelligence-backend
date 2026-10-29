"""Equipment / service discovery for a vendor already in the knowledge base ("Discover equipment" on a vendor page).

The vendor's own site is crawled and its capabilities extracted (same evidence rule as vendor discovery), then
compared with what the knowledge base holds:

- EXISTING facts (same category / certification / project): the new page is attached as extra evidence right away.
  Nothing about the fact changes, so no review is needed.
- NEW facts: proposed for curator review (an update proposal on the vendor).
- CONFLICTS (a different HQ country or city than recorded): proposed as a field change showing old and new values.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.ai.ports import AIError
from app.ai.service import AINotAvailable, CallContext
from app.core.exceptions import NotFound
from app.crawler.crawler import Crawler, Page
from app.discovery import pipeline
from app.discovery.classify import classify
from app.discovery.consolidate import Candidate
from app.discovery.evidence import EvidenceCheck
from app.discovery.pipeline import Researched, build_row, page_payload, pending_terms
from app.discovery.prompts import PROFILE
from app.discovery.schemas import VendorExtraction
from app.domain.enums import (
    ActorType,
    FactStatus,
    ProposalItemAction,
    ProposalOrigin,
    VendorStatus,
)
from app.domain.normalization import normalize_name, registrable_domain
from app.modules.audit.service import record_audit
from app.modules.imports.importer import ImportOptions, ParsedRow, Taxonomy, VendorListImporter, _fact_key
from app.modules.proposals.applier import Applier
from app.modules.vendors.models import (
    Vendor,
    VendorCertification,
    VendorEquipment,
    VendorProject,
    VendorService,
)
from app.workers.runner import JobContext

Scope = Literal["equipment", "services", "certifications", "industries", "projects"]
ALL_SCOPES: tuple[Scope, ...] = ("equipment", "services", "certifications", "industries", "projects")


class CapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_pages: int = Field(default=12, ge=3, le=30)
    # Extra pages to read (e.g. a product catalogue the crawler might not reach); must be on the vendor's own sites.
    extra_urls: list[str] = Field(default_factory=list, max_length=5)


def scoped(extraction: VendorExtraction, scope: list[str]) -> VendorExtraction:
    """Drops fact types outside the requested scope; a profile is always 'relevant'."""
    return extraction.model_copy(
        update={
            "is_relevant": True,
            "equipment": extraction.equipment if "equipment" in scope else [],
            "services": extraction.services if "services" in scope else [],
            "certifications": extraction.certifications if "certifications" in scope else [],
            "industries": extraction.industries if "industries" in scope else [],
            "projects": extraction.projects if "projects" in scope else [],
        }
    )


async def attach_existing_evidence(s: Any, vendor: Vendor, row: ParsedRow, tax: Taxonomy) -> int:
    """Adds this run's verified evidence to facts the vendor already has. Returns how many facts got evidence."""
    applier = Applier(s)
    attached = 0

    async def fact_id(model: Any, column: Any, value: Any) -> uuid.UUID | None:
        found: uuid.UUID | None = await s.scalar(
            select(model.id)
            .where(model.vendor_id == vendor.id, column == value, model.status == FactStatus.APPROVED)
            .limit(1)
        )
        return found

    checks: list[tuple[tuple[str, str], str, Any, Any, Any]] = []
    for code, _ in row.equipment:
        checks.append((_fact_key(ProposalItemAction.ADD_EQUIPMENT, {"category_code": code}), "vendor_equipment",
                       VendorEquipment, VendorEquipment.category_id, tax.ids[("equipment", code)]))  # fmt: skip
    for code, _ in row.services:
        checks.append((_fact_key(ProposalItemAction.ADD_SERVICE, {"category_code": code}), "vendor_services",
                       VendorService, VendorService.category_id, tax.ids[("services", code)]))  # fmt: skip
    for code in row.certifications:
        checks.append((_fact_key(ProposalItemAction.ADD_CERTIFICATION, {"certification_code": code}),
                       "vendor_certifications", VendorCertification, VendorCertification.certification_type_id,
                       tax.ids[("certifications", code)]))  # fmt: skip
    for key, table, model, column, value in checks:
        if (fid := await fact_id(model, column, value)) is not None and (evidence := row.evidence.get(key)):
            await applier.attach_evidence(table, fid, vendor.id, evidence)
            attached += 1
    if row.projects:
        existing = {
            normalize_name(name): pid
            for pid, name in (
                await s.execute(
                    select(VendorProject.id, VendorProject.project_name).where(
                        VendorProject.vendor_id == vendor.id, VendorProject.status == FactStatus.APPROVED
                    )
                )
            ).all()
        }
        for p in row.projects:
            key = _fact_key(ProposalItemAction.ADD_PROJECT, p)
            if (pid := existing.get(key[1])) and (evidence := row.evidence.get(key)):
                await applier.attach_evidence("vendor_projects", pid, vendor.id, evidence)
                attached += 1
    return attached


async def capability_discovery_handler(ctx: JobContext) -> dict[str, Any]:
    vendor_id = uuid.UUID(ctx.input["vendor_id"])
    scope: list[str] = list(ctx.input.get("scope") or ALL_SCOPES)
    req = CapabilityRequest.model_validate(ctx.input.get("request") or {})
    ai = ctx.ai
    if ai is None or not ai.configured:
        raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
    call = CallContext(ctx.tenant_id, ctx.user_id, ctx.job_id)

    async with ctx.session() as s:
        vendor = await s.get(Vendor, vendor_id)
        if vendor is None or vendor.status == VendorStatus.RETIRED:
            raise NotFound("Vendor not found")
        tax = await Taxonomy.load(s)
        name, website, country = vendor.display_name, vendor.website, vendor.hq_country
        domains = [d.domain for d in vendor.domains]
    primary = registrable_domain(website) if website else (domains[0] if domains else None)
    if primary is None:
        raise AIError("This vendor has no website or domain to research")
    sites = [primary, *(d for d in domains if d != primary)]

    await ctx.progress("researching", 5)
    pages: list[Page] = []
    async with pipeline.make_fetcher(ctx.settings) as fetcher:
        crawler = Crawler(fetcher, ctx.settings, ctx.sessionmaker)
        per_site = max(req.max_pages // len(sites), 3)
        for i, domain in enumerate(sites[:3]):
            starts = [website] if (website and i == 0) else [f"https://{domain}/"]
            starts += [u for u in req.extra_urls if registrable_domain(u) == domain]
            pages += await crawler.crawl_site(starts, max_pages=per_site, max_depth=2, domain=domain)
            await ctx.progress("researching", 5 + int(45 * (i + 1) / min(len(sites), 3)))
    if not pages:
        raise AIError("The vendor's website couldn't be read (down, blocked or disallowed by robots.txt)")

    await ctx.progress("extracting", 55)
    company = {"name": name, "website": website, "country": country}
    result = await ai.run(
        PROFILE, VendorExtraction, call, scope=scope, company=company, pages=page_payload(pages)
    )
    extraction = scoped(result.output, scope)
    cand = Candidate(name, website, primary, country, kinds={"manufacturer"}, relevance=1.0)
    researched = Researched(cand, vendor_id=vendor_id, pages=pages, extraction=extraction)

    await ctx.progress("classifying", 75)
    codes = await classify(pending_terms(researched, tax), tax, ai, call)
    check = EvidenceCheck.build(
        [(p.url, p.title, p.text) for p in pages] + [(p.final_url, p.title, p.text) for p in pages]
    )
    row, stats = build_row(researched, tax, codes, check)
    row.row_number, row.discarded, row.propose_changes = 1, None, True
    row.details["pages"] = [{"url": p.url, "title": p.title} for p in pages[:20]]
    row.details["pages_changed"] = sum(1 for p in pages if p.changed)

    await ctx.progress("proposing", 90)
    async with ctx.session() as s:
        vendor = await s.get(Vendor, vendor_id)
        assert vendor is not None
        evidence_added = await attach_existing_evidence(s, vendor, row, tax)
        row.details["evidence_added"] = evidence_added
        importer = VendorListImporter(
            s, tax, tenant_id=ctx.tenant_id, user_id=ctx.user_id, job_id=ctx.job_id, upload_id=None,
            auto_approve=False, options=ImportOptions(), origin=ProposalOrigin.AI_EQUIPMENT_DISCOVERY,
            evidence=pipeline.AI_EVIDENCE, link_to_tenant=False,
        )  # fmt: skip
        outcome = await importer.process(row)
        summary = {
            "vendor_id": str(vendor_id),
            "scope": scope,
            "pages_read": len(pages),
            "facts_found": stats["found"],
            "facts_verified": stats["kept"],
            "facts_unsupported": stats["unsupported"],
            "evidence_added_to_existing": evidence_added,
            "conflicts": len(row.details.get("conflicts", [])),
            "proposal_id": str(outcome.proposal_id) if outcome.proposal_id else None,
            "counts": {outcome.outcome.value: 1},
            "outcome": outcome.outcome.value,
        }
        record_audit(s, "discovery.vendor_researched", actor_type=ActorType.USER if ctx.user_id else ActorType.SYSTEM,
                     actor_id=ctx.user_id, tenant_id=ctx.tenant_id, entity_type="vendor", entity_id=vendor_id,
                     changes=summary)  # fmt: skip
        await s.commit()
    return summary
