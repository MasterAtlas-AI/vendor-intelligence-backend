"""The `vendor_discovery` job: from a sourcing request to proposals curators review.

1. plan        the AI turns the request into search queries                               (1 call)
2. search      each query runs with web search; companies + cited pages come back         (1 call per query)
3. consolidate de-duplicate by website/name, drop directories/news, rank, cap               (code)
4. match       known vendors are recognised (website, name, aliases) like an import row    (database)
5. research    each candidate's own website is crawled politely                           (crawler)
6. extract     the AI reads those pages and returns facts, each with URL + verbatim quote  (1 call per vendor)
7. check       facts whose quote isn't on a fetched page are dropped                        (code)
8. classify    capability terms -> taxonomy codes (local vocabulary, then one AI call)      (0-1 call)
9. propose     new vendors / new facts become proposals; every candidate gets a result row  (database)

The AI only proposes. Nothing it finds is approved automatically, whatever the organization's trust level.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.ai.ports import AIError, Citation
from app.ai.service import AINotAvailable, BudgetExceeded, CallContext
from app.core.config import Settings
from app.core.logging import get_logger
from app.crawler.crawler import Crawler, Page
from app.crawler.fetcher import Fetcher
from app.discovery.classify import classify, local_code
from app.discovery.consolidate import Candidate, consolidate
from app.discovery.estimate import query_count
from app.discovery.evidence import EvidenceCheck
from app.discovery.prompts import EXTRACT, PLAN, SEARCH
from app.discovery.schemas import DiscoveryRequest, SearchFindings, SearchPlan, VendorExtraction
from app.domain.enums import ActorType, ImportRowOutcome, ProposalItemAction, ProposalOrigin
from app.domain.normalization import normalize_country, normalize_name
from app.modules.audit.service import record_audit
from app.modules.imports.importer import ImportOptions, ParsedRow, Taxonomy, VendorListImporter, _fact_key
from app.modules.imports.pdf_extract import label_variants
from app.modules.tenants.policy import ai_controls
from app.modules.vendors.dedup import JUDGE_MIN_CONFIDENCE, judge, profile
from app.modules.vendors.models import Vendor
from app.workers.runner import JobContext

log = get_logger(__name__)

SEARCH_CONCURRENCY = 5  # ~37 s per search call (measured): 10 queries in ~1.5 min
PAGE_TEXT_BUDGET = 40_000  # characters of site text sent to the AI per vendor
MIN_CONFIDENCE = 0.5
AI_EVIDENCE = [{"kind": "ai", "title": "AI vendor discovery"}]


def make_fetcher(settings: Settings) -> Fetcher:
    """The HTTP fetcher a run uses (tests replace this to serve sites from memory)."""
    return Fetcher(settings)


@dataclass
class Researched:
    cand: Candidate
    vendor_id: Any = None  # matched existing vendor
    ambiguous: str | None = None
    pages: list[Page] = field(default_factory=list)
    extraction: VendorExtraction | None = None


def criteria_for(req: DiscoveryRequest, tax: Taxonomy) -> dict[str, Any]:
    """The request in words the AI understands (names, not codes)."""

    def names(kind: str, codes: list[str]) -> list[str]:
        return [tax.names.get((kind, c), c) for c in codes]

    criteria: dict[str, Any] = {
        "equipment": names("equipment", req.equipment_categories),
        "services": names("services", req.service_categories),
        "industries": names("industries", req.industries),
        "countries": req.countries,
        "regions": [r.replace("_", " ") for r in req.regions],
        "standards": req.standards,
        "certifications": names("certifications", req.certifications),
        "brief": req.brief,
    }
    return {k: v for k, v in criteria.items() if v}


def focus_terms(req: DiscoveryRequest, tax: Taxonomy) -> list[str]:
    """Words that mark a page as relevant to the request, so the crawler reads those pages first: standards, plus the
    chosen categories' short names and synonyms ("BB2", "barrel pump")."""
    terms = list(req.standards)
    for kind, codes in (("equipment", req.equipment_categories), ("services", req.service_categories)):
        labels = dict(tax.labels.get(kind, []))
        for code in codes:
            for label in labels.get(code, []):
                terms += [v for v in label_variants(label) if len(v) <= 40]
    return list(dict.fromkeys(t for t in terms if len(t.strip()) >= 3))[:20]


def page_payload(pages: list[Page], budget: int = PAGE_TEXT_BUDGET) -> list[dict[str, str]]:
    """Page texts for the extraction prompt, best pages first, within a character budget."""
    out, left = [], budget
    share = max(budget // max(len(pages), 1), 2_000)
    for page in pages:
        if left <= 0:
            break
        text = page.text[: min(share * 2, left)]
        out.append({"url": page.url, "title": page.title, "text": text})
        left -= len(text)
    return out


def flag_suspicious(row: ParsedRow, pages: list[Page]) -> None:
    """Lists pages where instruction-like text was removed before the AI read them, for the curator's attention."""
    if flagged := [p.url for p in pages if p.suspicious]:
        row.details["suspicious_pages"] = flagged[:20]


def build_row(
    r: Researched, tax: Taxonomy, codes: dict[tuple[str, str], str], check: EvidenceCheck
) -> tuple[ParsedRow, dict[str, int]]:
    """Turns one candidate's research into an importer row with per-fact evidence. Unsupported facts are dropped."""
    cand, ex = r.cand, r.extraction
    row = ParsedRow(
        row_number=0, name=cand.name, website=cand.website, domain=cand.domain, country=cand.country
    )
    row.vendor_id = r.vendor_id
    stats = {"found": 0, "kept": 0, "unsupported": 0, "unmapped": 0}
    sources = [{"url": c.url, "title": c.title} for c in cand.sources[:5]]
    row.details = {"website": cand.website, "country": cand.country, "kinds": sorted(cand.kinds),
                   "relevance": round(cand.relevance, 2), "why": cand.why, "sources": sources}  # fmt: skip
    if r.pages:
        row.default_evidence = [
            {"kind": "web", "url": r.pages[0].url, "title": r.pages[0].title[:300] or cand.name}
        ]
    if ex is None:
        return row, stats

    if ex.legal_name.strip() and r.vendor_id is None:
        row.name = " ".join(ex.legal_name.split())[:300]
    row.country = normalize_country(ex.hq_country or "") or cand.country
    row.city = (ex.hq_city or "").strip()[:120] or None
    row.description = " ".join(ex.description.split())[:2000]
    highlights: dict[str, list[str]] = {"equipment": [], "services": [], "certifications": []}

    def keep(action: ProposalItemAction, payload: dict[str, Any], evidence: list[Any], confidence: float,
             extra: dict[str, Any] | None = None) -> bool:  # fmt: skip
        stats["found"] += 1
        verified = check.verify(evidence)
        if not verified or confidence < MIN_CONFIDENCE:
            stats["unsupported"] += 1
            return False
        key = _fact_key(action, payload)
        if key in row.evidence:  # same fact twice (e.g. two products in one category): merge evidence
            row.evidence[key] = (row.evidence[key] + verified)[:3]
            return False
        row.evidence[key] = verified[:3]
        row.confidence[key] = Decimal(str(round(min(confidence, 0.95), 2)))
        if extra:
            row.extra[key] = extra
        stats["kept"] += 1
        return True

    def code_for(kind: str, *terms: str) -> str | None:
        found = local_code(tax, kind, *terms) or next(
            (codes[(kind, t)] for t in terms if (kind, t) in codes), None
        )
        if found is None:
            stats["unmapped"] += 1
        return found

    for e in ex.equipment:
        if (code := code_for("equipment", e.category_hint, e.name)) and keep(
            ProposalItemAction.ADD_EQUIPMENT, {"category_code": code}, e.evidence, e.confidence,
            {"model_series": (e.model_series or "")[:200] or None, "standards": [s[:80] for s in e.standards][:20]},
        ):  # fmt: skip
            row.equipment.append((code, e.name[:300]))
            highlights["equipment"].append(e.name[:120])
    for sv in ex.services:
        if (code := code_for("services", sv.category_hint, sv.name)) and keep(
            ProposalItemAction.ADD_SERVICE, {"category_code": code}, sv.evidence, sv.confidence
        ):
            row.services.append((code, sv.name[:300]))
            highlights["services"].append(sv.name[:120])
    for c in ex.certifications:
        extra = {"certificate_number": c.certificate_number[:120]} if c.certificate_number else None
        if (code := code_for("certifications", c.name)) and keep(
            ProposalItemAction.ADD_CERTIFICATION,
            {"certification_code": code},
            c.evidence,
            c.confidence,
            extra,
        ):
            row.certifications.append(code)
            highlights["certifications"].append(c.name[:120])
    for ind in ex.industries:
        if (code := code_for("industries", ind.name)) and keep(
            ProposalItemAction.ADD_INDUSTRY, {"industry_code": code}, ind.evidence, ind.confidence
        ):
            row.industries.append(code)
    for p in ex.projects:
        payload = {
            "project_name": p.project_name.strip()[:300],
            "client": (p.client or "").strip()[:200] or None,
            "country": normalize_country(p.country or ""),
            "year": p.year if p.year and 1900 <= p.year <= 2100 else None,
            "scope": p.scope.strip()[:2000],
        }
        if normalize_name(str(payload["project_name"])) and keep(
            ProposalItemAction.ADD_PROJECT, payload, p.evidence, p.confidence
        ):
            row.projects.append(payload)

    row.details["highlights"] = {k: v[:6] for k, v in highlights.items() if v}
    row.details["facts"] = stats
    if not ex.is_relevant:
        row.discarded = f"Not a match: {ex.relevance_reason.strip()[:300]}"
    elif r.vendor_id is None and stats["kept"] == 0:
        row.discarded = "Its website didn't state any capability we could verify"
    return row, stats


def pending_terms(r: Researched, tax: Taxonomy) -> list[tuple[str, str]]:
    """Capability terms the local vocabulary can't place, for the one AI classification call."""
    ex = r.extraction
    if ex is None or not ex.is_relevant:
        return []
    out = []
    for e in ex.equipment:
        if not local_code(tax, "equipment", e.category_hint, e.name):
            out.append(("equipment", e.category_hint))
    for s in ex.services:
        if not local_code(tax, "services", s.category_hint, s.name):
            out.append(("services", s.category_hint))
    out += [
        ("certifications", c.name) for c in ex.certifications if not local_code(tax, "certifications", c.name)
    ]
    out += [("industries", i.name) for i in ex.industries if not local_code(tax, "industries", i.name)]
    return out


async def vendor_discovery_handler(ctx: JobContext) -> dict[str, Any]:
    req = DiscoveryRequest.model_validate(ctx.input["request"])
    ai = ctx.ai
    if ai is None or not ai.configured:
        raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
    call = CallContext(ctx.tenant_id, ctx.user_id, ctx.job_id)
    async with ctx.session() as s:
        tax = await Taxonomy.load(s)
    criteria = criteria_for(req, tax)
    focus = focus_terms(req, tax)
    began, timings = (
        time.monotonic(),
        dict[str, int](),
    )  # seconds from the start of the run to each stage, for tuning

    async def progress(stage: str, percent: int) -> None:
        timings.setdefault(stage, round(time.monotonic() - began))
        await ctx.progress(stage, percent)

    # 1. plan
    await progress("planning", 3)
    count = query_count(req)
    plan = await ai.run(PLAN, SearchPlan, call, criteria=criteria, count=count)
    queries = list(dict.fromkeys(q.strip()[:200] for q in plan.output.queries if q.strip()))[:count]
    if not queries:
        raise AIError("The AI returned no search queries")

    # 2. search (limited to the organization's approved public sources when it chose that)
    await progress("searching", 8)
    async with ctx.session() as policy_session:
        allowed = (await ai_controls(policy_session, ctx.tenant_id)).allowed_domains()
    findings: list[tuple[list[Any], list[Citation]]] = []
    failed: list[str] = []
    sem = asyncio.Semaphore(SEARCH_CONCURRENCY)

    async def search(query: str) -> None:
        async with sem:
            try:
                res = await ai.run(
                    SEARCH,
                    SearchFindings,
                    call,
                    web_search=True,
                    allowed_domains=allowed,
                    query=query,
                    criteria=criteria,
                )
            except BudgetExceeded:
                raise
            except AIError as exc:
                log.warning("discovery.search_failed", query=query, error=str(exc)[:200])
                failed.append(query)
                return
            findings.append((res.output.companies, res.citations))
            await progress("searching", 8 + int(27 * (len(findings) + len(failed)) / len(queries)))

    await asyncio.gather(*(search(q) for q in queries))
    if not findings:
        raise AIError(f"All {len(queries)} web searches failed")
    wants_distributors = "distributor" in req.brief.lower()
    kept, dropped = consolidate(findings, limit=req.max_candidates, allow_distributors=wants_distributors)

    # 3. match against the knowledge base
    await progress("matching", 36)
    researched = [Researched(c) for c in kept]
    async with ctx.session() as s:
        probe = VendorListImporter(s, tax, tenant_id=ctx.tenant_id, user_id=ctx.user_id, job_id=None, upload_id=None,
                                   auto_approve=False, options=ImportOptions(), dry_run=True)  # fmt: skip
        for r in researched:
            probe_row = ParsedRow(
                1, name=r.cand.name, website=r.cand.website, domain=r.cand.domain, country=r.cand.country
            )
            vendor, ambiguity = await probe.match(probe_row)
            r.vendor_id, r.ambiguous = (vendor.id if vendor else None), ambiguity
            # Grey zone (similar name, not certain): let the AI judge rather than proposing a duplicate.
            if vendor is None and not ambiguity and (maybe := probe_row.details.get("possible_duplicates")):
                existing = await s.get(Vendor, uuid.UUID(maybe[0]["vendor_id"]))
                found = {"name": r.cand.name, "websites": [r.cand.domain] if r.cand.domain else [],
                         "country": r.cand.country, "description": r.cand.why}  # fmt: skip
                if existing is not None:
                    try:
                        verdict = await judge(ai, call, await profile(s, existing), found)
                    except (AIError, BudgetExceeded) as exc:
                        log.warning("discovery.judge_failed", error=str(exc)[:200])
                    else:
                        if verdict.relationship == "same" and verdict.confidence >= JUDGE_MIN_CONFIDENCE:
                            r.vendor_id = existing.id

    # 4-6. research and extract, a few vendors at a time
    to_research = [r for r in researched if not r.ambiguous and r.cand.website
                   and (r.vendor_id is None or req.include_existing)]  # fmt: skip
    budget_hit = False
    done = 0
    sem = asyncio.Semaphore(ctx.settings.discovery_research_concurrency)
    async with make_fetcher(ctx.settings) as fetcher:
        crawler = Crawler(fetcher, ctx.settings, ctx.sessionmaker)

        async def research(r: Researched) -> None:
            nonlocal budget_hit, done
            async with sem:
                if budget_hit:
                    r.cand.discarded = "Not researched: the AI budget ran out"
                    return
                starts = [r.cand.website, *(c.url for c in r.cand.sources)]
                r.pages = await crawler.crawl_site([u for u in starts if u], max_pages=req.pages_per_vendor,
                                                   max_depth=2, domain=r.cand.domain, focus=focus)  # fmt: skip
                if not r.pages:
                    r.cand.discarded = (
                        "Its website couldn't be read (down, blocked or disallowed by robots.txt)"
                    )
                else:
                    company = {"name": r.cand.name, "website": r.cand.website, "country": r.cand.country}
                    try:
                        res = await ai.run(EXTRACT, VendorExtraction, call, criteria=criteria, company=company,
                                           pages=page_payload(r.pages))  # fmt: skip
                        r.extraction = res.output
                    except BudgetExceeded:
                        budget_hit = True
                        r.cand.discarded = "Not researched: the AI budget ran out"
                    except AIError as exc:
                        r.cand.discarded = f"Couldn't analyse its website: {str(exc)[:200]}"
                done += 1
                await progress("researching", 40 + int(45 * done / max(len(to_research), 1)))

        await progress("researching", 40)
        await asyncio.gather(*(research(r) for r in to_research))

    # 7-8. evidence check and classification
    await progress("classifying", 88)
    pending = [t for r in to_research for t in pending_terms(r, tax)]
    codes: dict[tuple[str, str], str] = {}
    if pending and not budget_hit:
        try:
            codes = await classify(pending, tax, ai, call)
        except (AIError, BudgetExceeded) as exc:
            log.warning("discovery.classify_failed", error=str(exc)[:200])

    rows: list[ParsedRow] = []
    for r in researched:
        if r.ambiguous:
            row = ParsedRow(
                0, name=r.cand.name, website=r.cand.website, domain=r.cand.domain, country=r.cand.country
            )
            row.details = {"website": r.cand.website, "why": r.cand.why}
        elif r.cand.website is None and r.vendor_id is None:
            row = ParsedRow(0, name=r.cand.name, country=r.cand.country)
            row.discarded = "No official website found"
        else:
            check = EvidenceCheck.build(
                [(p.url, p.title, p.text) for p in r.pages]
                + [(p.final_url, p.title, p.text) for p in r.pages]
            )
            row, _ = build_row(r, tax, codes, check)
            flag_suspicious(row, r.pages)
            if r.cand.discarded and not row.discarded:
                row.discarded = r.cand.discarded
            if r.vendor_id is not None and r not in to_research:
                row.details["note"] = "Already in the knowledge base (not re-researched)"
        rows.append(row)
    for cand in dropped:
        row = ParsedRow(0, name=cand.name, website=cand.website, domain=cand.domain, country=cand.country)
        row.discarded = cand.discarded or "Not a candidate"
        row.details = {"website": cand.website, "kinds": sorted(cand.kinds), "why": cand.why}
        rows.append(row)

    # 9. propose
    await progress("proposing", 94)
    async with ctx.session() as s:
        importer = VendorListImporter(
            s, tax, tenant_id=ctx.tenant_id, user_id=ctx.user_id, job_id=ctx.job_id, upload_id=None,
            auto_approve=False,  # AI findings always go to a curator
            options=ImportOptions(), origin=ProposalOrigin.AI_VENDOR_DISCOVERY, evidence=AI_EVIDENCE,
            link_to_tenant=False,
        )  # fmt: skip
        counts = {o.value: 0 for o in ImportRowOutcome}
        for n, row in enumerate(rows, start=1):
            row.row_number = n
            result = await importer.process(row)
            counts[result.outcome.value] += 1
        summary = {
            "queries": queries,
            "failed_queries": len(failed),
            "candidates": len(kept),
            "researched": len(to_research),
            "pages_read": sum(len(r.pages) for r in researched),
            "budget_ran_out": budget_hit,
            "counts": {k: v for k, v in counts.items() if v},
            "stage_started_s": timings | {"done": round(time.monotonic() - began)},
        }
        record_audit(s, "discovery.completed", actor_type=ActorType.USER if ctx.user_id else ActorType.SYSTEM,
                     actor_id=ctx.user_id, tenant_id=ctx.tenant_id, entity_type="job", entity_id=ctx.job_id,
                     changes={k: v for k, v in summary.items() if k != "queries"})  # fmt: skip
        await s.commit()
    return summary
