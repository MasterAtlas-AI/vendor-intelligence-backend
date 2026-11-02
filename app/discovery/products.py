"""Full product details for a known vendor: read its website (product pages and PDF datasheets) and extract every
product line with its technical data, then propose it for review.

Unlike the general research, this reads more pages, sends them to the AI in batches (so nothing is cut off) and asks
for specifications: performance ranges, construction, materials, media, standards, applications. The evidence rule
still applies: a product is kept only if its quote appears on a page we fetched, and each specification only if its
value appears on one of the product's cited pages. Products that already exist are completed, not duplicated (the
applier merges new specifications into them).
"""

import asyncio
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.ai.ports import AIError, Usage
from app.ai.pricing import call_cost
from app.ai.prompts import DATA_RULE, Prompt, register
from app.ai.service import AINotAvailable, BudgetExceeded, CallContext
from app.core.config import Settings
from app.core.exceptions import NotFound
from app.core.logging import get_logger
from app.crawler.crawler import Crawler, Page
from app.crawler.urls import normalize_url
from app.discovery import pipeline
from app.discovery.classify import classify, local_code
from app.discovery.evidence import EvidenceCheck
from app.discovery.schemas import DiscoveryEstimate, FactEvidence
from app.domain.enums import ActorType, ProposalItemAction, ProposalKind, ProposalOrigin, VendorStatus
from app.domain.normalization import normalize_name, registrable_domain
from app.modules.audit.service import record_audit
from app.modules.imports.importer import Taxonomy
from app.modules.proposals.service import NewItem, ProposalService
from app.modules.vendors.models import Vendor
from app.workers.runner import JobContext

log = get_logger(__name__)

Depth = Literal["standard", "deep"]
PAGES: dict[Depth, int] = {"standard": 25, "deep": 60}
CRAWL_DEPTH: dict[Depth, int] = {"standard": 2, "deep": 3}
BATCH_CHARS = 40_000  # site text per AI call
BATCH_CONCURRENCY = 3
MIN_CONFIDENCE = 0.5
EXTRACT_TOKENS = (14_000, 4_000)  # per batch (input, output); recalibrate from ai_usage_events


class ExtractedSpec(BaseModel):
    name: str = Field(description="What is specified, e.g. 'Max flow', 'Max head', 'Casing material'.")
    value: str = Field(description="Exactly as the page states it, e.g. '2,600', '-40 to 450', 'Duplex SS'.")
    unit: str | None = Field(description="Unit as stated (m3/h, bar, degC, kW, mm...), or null.")


class ExtractedProduct(BaseModel):
    name: str
    category_hint: str = Field(
        description="What kind of equipment it is, e.g. 'API 610 OH2 centrifugal pump'."
    )
    model_series: str | None = Field(description="Model or series code, e.g. 'EAP/EAPK series'.")
    overview: str | None = Field(description="One or two factual sentences on what it is and is used for.")
    standards: list[str]
    applications: list[str] = Field(
        description="Industries or services it is for, e.g. 'Refinery', 'Seawater'."
    )
    specs: list[ExtractedSpec]
    evidence: list[FactEvidence] = Field(description="Pages showing the product; quotes must be verbatim.")
    confidence: float


class ProductCatalog(BaseModel):
    products: list[ExtractedProduct]


PRODUCTS = register(
    Prompt(
        name="product_catalog",
        version=1,
        reasoning_effort="low",
        instructions=f"""You are a technical procurement analyst for industrial equipment (oil & gas, LNG, power, process).
From the vendor's web pages and datasheets given, list every product or product line the vendor itself makes or
packages, with its technical data. Pages may come in several batches; report what this batch shows.
- One entry per product line / model series. Skip accessories, spare parts and generic marketing pages.
- specs: every technical value stated, one per entry, with the value and unit exactly as written. Cover what applies:
  type and configuration (e.g. OH2, between-bearings, stages, orientation), performance ranges (min/max flow or
  capacity, head, pressure, temperature, speed, power, efficiency), sizes and connections, construction (casing,
  impeller, sealing, bearings), materials, media or fluids handled, design and test pressure, weights and dimensions,
  hazardous-area or other ratings, delivery or lead time if stated.
- Use clear names ("Max flow", "Max head", "Max working pressure", "Max fluid temperature", "Casing material").
- standards: e.g. "API 610", "ISO 13709", "ATEX". applications: industries or services named for the product.
- evidence: the page URL and a verbatim quote (at least 30 characters) that shows the product and its key numbers.
- Never invent values, convert units or fill gaps from general knowledge. If a value isn't on the page, leave it out.
{DATA_RULE}""",
        template="Vendor:\n{company}\n\nPages:\n{pages}",
    )
)


def estimate(depth: Depth, settings: Settings) -> DiscoveryEstimate:
    batches = max(1, PAGES[depth] * 3_000 // BATCH_CHARS)  # pages average a few thousand characters of text
    usage = Usage(settings.openai_model, EXTRACT_TOKENS[0], 0, EXTRACT_TOKENS[1])
    typical = float(call_cost(usage, settings) * batches * Decimal("1.2"))  # plus classification
    return DiscoveryEstimate(queries=0, web_searches=0, candidates=1, typical_cost_usd=round(typical, 2),
                             max_cost_usd=round(typical * 1.6, 2))  # fmt: skip


def batches(pages: list[Page]) -> list[list[dict[str, str]]]:
    """Page texts in groups of about BATCH_CHARS characters; a very long page (a datasheet) gets a batch of its own."""
    out: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    size = 0
    for p in pages:
        text = p.text[:BATCH_CHARS]
        if current and size + len(text) > BATCH_CHARS:
            out.append(current)
            current, size = [], 0
        current.append({"url": p.final_url or p.url, "title": p.title, "text": text})
        size += len(text)
    if current:
        out.append(current)
    return out


def _key(p: ExtractedProduct) -> str:
    return normalize_name(p.model_series or p.name)


def merge(found: list[ExtractedProduct]) -> list[ExtractedProduct]:
    """The same product reported by several batches becomes one entry with everything it was found with."""
    merged: dict[str, ExtractedProduct] = {}
    for p in found:
        k = _key(p)
        if not k:
            continue
        if (m := merged.get(k)) is None:
            merged[k] = p.model_copy(deep=True)
            continue
        names = {normalize_name(s.name) for s in m.specs}
        m.specs += [s for s in p.specs if normalize_name(s.name) not in names]
        m.standards += [s for s in p.standards if s not in m.standards]
        m.applications += [a for a in p.applications if a not in m.applications]
        m.evidence += p.evidence
        m.overview = m.overview or p.overview
        m.model_series = m.model_series or p.model_series
        m.confidence = max(m.confidence, p.confidence)
    return list(merged.values())


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def supported_specs(product: ExtractedProduct, page_texts: dict[str, str]) -> tuple[dict[str, str], int]:
    """Specs whose value appears on one of the product's cited pages, as {name: "value unit"}; and how many were
    dropped. Numbers must appear as written (thousands separators ignored)."""
    cited = [page_texts[k] for ev in product.evidence if (k := normalize_url(ev.url)) and k in page_texts]
    haystack = " ".join(cited)
    plain = haystack.replace(",", "")
    kept: dict[str, str] = {}
    dropped = 0
    for s in product.specs:
        value = _fold(s.value)
        numbers = re.findall(r"\d+(?:\.\d+)?", value.replace(",", ""))
        ok = bool(value) and (value in haystack or (bool(numbers) and all(n in plain for n in numbers)))
        name = s.name.strip()[:80]
        if ok and name and name not in kept:
            kept[name] = (
                f"{s.value.strip()} {s.unit.strip()}".strip()[:200] if s.unit else s.value.strip()[:200]
            )
        else:
            dropped += 1
    return kept, dropped


async def product_catalog_handler(ctx: JobContext) -> dict[str, Any]:
    vendor_id = uuid.UUID(ctx.input["vendor_id"])
    depth: Depth = ctx.input.get("depth", "standard")
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
        raise AIError("This vendor has no website to read; add it first")

    await ctx.progress("reading website", 5)
    async with pipeline.make_fetcher(ctx.settings) as fetcher:
        crawler = Crawler(fetcher, ctx.settings, ctx.sessionmaker)
        pages = await crawler.crawl_site([website or f"https://{primary}/"], max_pages=PAGES[depth],
                                         max_depth=CRAWL_DEPTH[depth], domain=primary)  # fmt: skip
    pages = [p for p in pages if p.text.strip()]
    if not pages:
        raise AIError("The vendor's website couldn't be read (down, blocked or disallowed by robots.txt)")

    groups = batches(pages)
    await ctx.progress("extracting products", 35)
    company = {"name": name, "website": website or primary, "country": country}
    sem = asyncio.Semaphore(BATCH_CONCURRENCY)
    found: list[ExtractedProduct] = []
    done = failed = 0

    async def extract(group: list[dict[str, str]]) -> None:
        nonlocal done, failed
        async with sem:
            try:
                res = await ai.run(PRODUCTS, ProductCatalog, call, company=company, pages=group)
                found.extend(res.output.products)
            except BudgetExceeded:
                raise
            except AIError as exc:
                failed += 1
                log.warning("products.batch_failed", vendor_id=str(vendor_id), error=str(exc)[:200])
        done += 1
        await ctx.progress("extracting products", 35 + int(45 * done / len(groups)))

    await asyncio.gather(*(extract(g) for g in groups))
    if failed == len(groups):
        raise AIError("Reading the product pages failed")

    await ctx.progress("checking evidence", 82)
    products = merge(found)
    check = EvidenceCheck.build(
        [(p.url, p.title, p.text) for p in pages] + [(p.final_url, p.title, p.text) for p in pages]
    )
    page_texts = check.pages
    # What isn't equipment may be a service (engineering, inspection, analysis...): both are tried, one AI call.
    pending = [
        (kind, p.category_hint)
        for p in products
        if not local_code(tax, "equipment", p.category_hint, p.name)
        for kind in ("equipment", "services")
    ]
    codes = await classify(pending, tax, ai, call)

    items: list[NewItem] = []
    stats = {
        "found": len(products),
        "kept": 0,
        "unsupported": 0,
        "unmapped": 0,
        "specs": 0,
        "specs_dropped": 0,
        "services": 0,
    }
    not_kept: list[dict[str, Any]] = []  # shown to the curator, so nothing found disappears silently

    def drop(p: ExtractedProduct, reason: str) -> None:
        not_kept.append({"name": p.name.strip()[:300], "model_series": p.model_series, "category": p.category_hint,
                         "specs": len(p.specs), "reason": reason})  # fmt: skip

    for p in products:
        verified = check.verify(p.evidence)
        if not verified:
            stats["unsupported"] += 1
            drop(p, "Its quote wasn't found on the page it cites")
            continue
        if p.confidence < MIN_CONFIDENCE:
            stats["unsupported"] += 1
            drop(p, f"The AI wasn't sure enough ({p.confidence:.0%})")
            continue
        code = local_code(tax, "equipment", p.category_hint, p.name) or codes.get(
            ("equipment", p.category_hint)
        )
        if code is None:
            service = local_code(tax, "services", p.category_hint, p.name) or codes.get(
                ("services", p.category_hint)
            )
            if service is None:
                stats["unmapped"] += 1
                drop(p, "No matching equipment or service category in the taxonomy")
                continue
            stats["kept"] += 1
            stats["services"] += 1
            items.append(NewItem(ProposalItemAction.ADD_SERVICE, {"category_code": service, "name": p.name.strip()[:300]},
                                 evidence=verified[:3], confidence=Decimal(str(round(min(p.confidence, 0.95), 2)))))  # fmt: skip
            continue
        specs, dropped = supported_specs(p, page_texts)
        if p.overview and p.overview.strip():
            specs = {"Overview": p.overview.strip()[:500], **specs}
        stats["specs"] += len(specs)
        stats["specs_dropped"] += dropped
        stats["kept"] += 1
        items.append(
            NewItem(
                ProposalItemAction.ADD_EQUIPMENT,
                {
                    "category_code": code,
                    "name": p.name.strip()[:300],
                    "model_series": (p.model_series or "").strip()[:200] or None,
                    "standards": list(dict.fromkeys(s.strip()[:80] for s in p.standards if s.strip()))[:20],
                    "applications": list(dict.fromkeys(a.strip()[:120] for a in p.applications if a.strip()))[
                        :20
                    ],
                    "specs": specs,
                },
                evidence=verified[:3],
                confidence=Decimal(str(round(min(p.confidence, 0.95), 2))),
            )
        )

    await ctx.progress("proposing", 92)
    proposal_id = None
    async with ctx.session() as s:
        if items:
            proposal = await ProposalService(s, None).submit(
                ProposalKind.UPDATE_VENDOR, ProposalOrigin.AI_EQUIPMENT_DISCOVERY, items,
                title=f"Products for {name}", subject_vendor_id=vendor_id, origin_tenant_id=ctx.tenant_id,
                origin_user_id=ctx.user_id, origin_job_id=ctx.job_id,
            )  # fmt: skip
            proposal_id = proposal.id
        v = await s.get(Vendor, vendor_id)
        if v is not None:
            v.last_verified_at = datetime.now(UTC)
        summary = {
            "vendor_id": str(vendor_id),
            "vendor_name": name,
            "depth": depth,
            "pages_read": len(pages),
            "batches": len(groups),
            "products_found": stats["found"],
            "products_kept": stats["kept"],
            "products_unsupported": stats["unsupported"],
            "products_unmapped": stats["unmapped"],
            "specs_kept": stats["specs"],
            "specs_unsupported": stats["specs_dropped"],
            "services_kept": stats["services"],
            "proposal_id": str(proposal_id) if proposal_id else None,
        }
        record_audit(s, "discovery.products_researched", actor_type=ActorType.USER if ctx.user_id else ActorType.SYSTEM,
                     actor_id=ctx.user_id, tenant_id=ctx.tenant_id, entity_type="vendor", entity_id=vendor_id,
                     changes=summary)  # fmt: skip
        await s.commit()
    return {**summary, "not_kept": not_kept[:100]}
