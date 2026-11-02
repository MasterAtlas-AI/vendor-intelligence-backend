"""Fill missing vendor basics with AI web search: website, headquarters country or description (data-quality lists).

For each selected vendor the AI searches the web for the one missing field. An answer is only proposed when it holds
up:
- website: a usable URL, not a directory or social site, not a domain another vendor already owns, and either the home
  page names the company when we open it, or the AI's search results cite that site and it is confident;
- country: a real ISO country, a confident answer and a cited source;
- description: a factual 1-3 sentence summary (40-600 characters), a confident answer and a cited source.
Accepted answers go to Approvals as ordinary proposals (nothing changes without a curator); the job result lists every
vendor with what happened, so the page can show it and approve in place.
"""

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.ai.ports import AIError, Citation
from app.ai.prompts import DATA_RULE, Prompt, register
from app.ai.service import AINotAvailable, BudgetExceeded, CallContext
from app.core.config import Settings
from app.core.logging import get_logger
from app.crawler.crawler import Crawler
from app.crawler.html_text import html_to_text
from app.discovery import pipeline
from app.domain.enums import FactStatus, ProposalItemAction, ProposalKind, ProposalOrigin, VendorStatus
from app.domain.normalization import normalize_country, normalize_name, normalize_website, registrable_domain
from app.modules.proposals.payloads import AddContact
from app.modules.proposals.service import NewItem, ProposalService
from app.modules.vendors.models import Vendor, VendorContact, VendorDomain, VendorEquipment
from app.workers.runner import JobContext

log = get_logger(__name__)

Field_ = Literal["website", "hq_country", "description", "contacts"]
FIELDS: tuple[Field_, ...] = ("website", "hq_country", "description", "contacts")
CONTACT_PAGES = 8  # home, contact and about pages of the vendor's own site

MAX_VENDORS = 25
CONCURRENCY = 5
MIN_CONFIDENCE = 0.6
# Answers we can't check ourselves (anything but a website's home page) need a cited source and more confidence.
MIN_CONFIDENCE_CITED_ONLY = 0.75
DESCRIPTION_LENGTH = (40, 600)
# Words too common in company names to prove a page belongs to this company.
GENERIC = {
    "engineering", "industries", "industrial", "group", "company", "international", "global", "services",
    "solutions", "systems", "technologies", "technology", "equipment", "products", "manufacturing", "trading",
    "pumps", "valves", "energy", "power", "process", "india", "limited", "private", "the", "and",
}  # fmt: skip
NOT_COMPANY_SITES = {
    "linkedin.com", "facebook.com", "wikipedia.org", "indiamart.com", "tradeindia.com", "alibaba.com",
    "made-in-china.com", "kompass.com", "zoominfo.com", "dnb.com", "bloomberg.com", "crunchbase.com",
    "justdial.com", "yellowpages.com", "europages.com", "thomasnet.com", "google.com", "youtube.com",
}  # fmt: skip

_ROLE = "You are a procurement research analyst for industrial equipment and services."
_SAME_COMPANY = """- Make sure it is the same company: match the name and, when given, the website, country, city and what it
  supplies. Many companies share similar names; if you can't tell them apart, return null.
- confidence: 0 to 1. Use 0.9+ only when name, location and products all match."""


class WebsiteFinding(BaseModel):
    website: str | None = Field(description="The company's own home page URL, or null if not found.")
    confidence: float = Field(
        ge=0, le=1, description="How sure you are this is the same company's official site."
    )
    reason: str = Field(
        description="One sentence: why this is (or why you couldn't find) the company's site."
    )


class CountryFinding(BaseModel):
    country: str | None = Field(description="ISO 3166 alpha-2 code of the headquarters country, or null.")
    city: str | None = Field(description="Headquarters city, or null if unknown.")
    source_url: str | None = Field(description="The page that states the headquarters location.")
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(description="One sentence: where the headquarters is and how you know.")


class DescriptionFinding(BaseModel):
    description: str | None = Field(
        description="1-3 factual sentences on what the company makes or does, or null if not found."
    )
    source_url: str | None = Field(
        description="The page the description is based on (ideally the company's own)."
    )
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(description="One sentence: which source you used.")


FIND_WEBSITE = register(
    Prompt(
        name="vendor_website_lookup",
        version=1,
        reasoning_effort="low",
        instructions=f"""{_ROLE}
Use web search to find the official website (home page) of the company described.
- It must be the company's own site, not a directory, marketplace, distributor listing, social network or news page.
- A parent group's site is acceptable only if the company has no site of its own; say so in the reason.
{_SAME_COMPANY}
- Never invent a URL. If you can't find it, website is null.
{DATA_RULE}""",
        template="Company:\n{company}",
    )
)

FIND_COUNTRY = register(
    Prompt(
        name="vendor_country_lookup",
        version=1,
        reasoning_effort="low",
        instructions=f"""{_ROLE}
Use web search to find where the company described has its headquarters (registered or head office).
- Prefer the company's own website (contact or about page), then official company registers.
- Not a sales office, factory or distributor location unless it is also the head office.
{_SAME_COMPANY}
- Never guess from the company name or language. If you can't find it, country is null.
{DATA_RULE}""",
        template="Company:\n{company}",
    )
)

FIND_DESCRIPTION = register(
    Prompt(
        name="vendor_description_lookup",
        version=1,
        reasoning_effort="low",
        instructions=f"""{_ROLE}
Use web search to write a short, factual description of the company described for a vendor database.
- 1 to 3 sentences, plain language: what it makes or does, for which industries, and where it is based if known.
- Only facts stated by a source, ideally the company's own website. No marketing language, no superlatives, no
  opinions.
{_SAME_COMPANY}
- If you can't find reliable information, description is null.
{DATA_RULE}""",
        template="Company:\n{company}",
    )
)

Status = Literal["proposed", "not_found", "rejected", "already_filled", "failed"]
STATUSES: tuple[Status, ...] = ("proposed", "not_found", "rejected", "already_filled", "failed")


class LookupRow(BaseModel):
    vendor_id: uuid.UUID
    name: str
    status: Status
    value: str | None = Field(
        default=None, description="What was found (website URL, country code, description)."
    )
    city: str | None = None
    source_url: str | None = None
    confidence: float | None = None
    checked: Literal["home_page", "citation"] | None = None
    reason: str = ""
    proposal_id: uuid.UUID | None = None
    # When the website already belongs to another vendor: that vendor, so a curator can compare or merge.
    other_vendor_id: uuid.UUID | None = None
    other_vendor_name: str | None = None
    found: list[str] = Field(
        default_factory=list, description="Contacts: each email, phone or address proposed."
    )


@dataclass
class Accepted:
    items: list[NewItem]


def estimate_usd(vendors: int, settings: Settings) -> Decimal:
    """Typical cost: one web search plus a few thousand tokens per vendor."""
    per = Decimal(str(settings.ai_price_per_web_search)) + Decimal("0.004")
    return (per * vendors).quantize(Decimal("0.01"))


def name_tokens(name: str) -> list[str]:
    return [t for t in normalize_name(name).split() if len(t) >= 3 and t not in GENERIC]


def page_names_company(text: str, tokens: list[str]) -> bool:
    """The home page mentions the company: its distinctive name words (all of them, or the first two)."""
    if not tokens:
        return False
    folded = " " + re.sub(r"[^a-z0-9]+", " ", normalize_name(text)) + " "
    return all(f" {t} " in folded for t in tokens[:2])


async def _home_page_text(url: str, settings: Settings) -> str | None:
    async with pipeline.make_fetcher(settings) as fetcher:
        res = await fetcher.fetch(url)
        if not res.ok or res.is_pdf:
            return None
        doc = html_to_text(res.body.decode("utf-8", errors="replace"))
        return f"{doc.title} {doc.description} {doc.text[:20000]}"


def _web(url: str | None, title: str, snippet: str) -> list[dict[str, Any]]:
    return [{"kind": "web", "url": url, "title": title, "snippet": snippet[:2000]}] if url else []


def _cited_source(source_url: str | None, citations: list[Citation]) -> str | None:
    """The source to show as evidence: the AI's stated page if its search actually cited that site, else the first
    citation. None when the search cited nothing."""
    if source_url and any(registrable_domain(c.url) == registrable_domain(source_url) for c in citations):
        return source_url
    return citations[0].url if citations else None


async def _check_website(
    found: WebsiteFinding, citations: list[Citation], vendor: dict[str, Any], row: LookupRow, ctx: JobContext
) -> Accepted | None:
    website = normalize_website(found.website) if found.website else None
    domain = registrable_domain(website) if website else None
    if not website or not domain:
        return None
    row.value = website
    if domain in NOT_COMPANY_SITES:
        row.status, row.reason = (
            "rejected",
            f"{domain} is a directory or social site, not the company's own site",
        )
        return None
    if found.confidence < MIN_CONFIDENCE:
        row.status, row.reason = "rejected", f"Not sure enough ({found.confidence:.0%}): {row.reason}"
        return None
    async with ctx.session() as s:
        owner = (
            await s.execute(
                select(Vendor.id, Vendor.display_name)
                .join(VendorDomain, VendorDomain.vendor_id == Vendor.id)
                .where(VendorDomain.domain == domain)
            )
        ).first()
    if owner is not None and owner.id != vendor["id"]:
        row.status, row.reason = (
            "rejected",
            f"{domain} already belongs to {owner.display_name} (possible duplicate)",
        )
        row.other_vendor_id, row.other_vendor_name = owner.id, owner.display_name
        return None
    text = await _home_page_text(website, ctx.settings)
    cited = any(registrable_domain(c.url) == domain for c in citations)
    if text is not None and page_names_company(text, name_tokens(vendor["name"])):
        row.checked = "home_page"
    elif cited and found.confidence >= MIN_CONFIDENCE_CITED_ONLY:
        row.checked = "citation"
    else:
        row.status = "rejected"
        row.reason = (
            "The home page doesn't name the company"
            if text is not None
            else "Couldn't open the site to confirm it, and the search results don't back it strongly enough"
        )
        return None
    how = "home page names the company" if row.checked == "home_page" else "cited by the AI's web search"
    item = NewItem(ProposalItemAction.UPDATE_VENDOR_FIELD, {"field": "website", "value": website},
                   evidence=_web(website, f"AI website lookup ({how})", row.reason),
                   confidence=Decimal(str(row.confidence or 0)))  # fmt: skip
    return Accepted([item])


def _sourced(
    confidence: float, source_url: str | None, citations: list[Citation], row: LookupRow
) -> str | None:
    """The evidence URL for an answer we can't check ourselves, or None (with the row marked rejected)."""
    source = _cited_source(source_url, citations)
    if source is None:
        row.status, row.reason = "rejected", "The web search cited no source for this answer"
        return None
    if confidence < MIN_CONFIDENCE_CITED_ONLY:
        row.status, row.reason = "rejected", f"Not sure enough ({confidence:.0%}): {row.reason}"
        return None
    row.source_url, row.checked = source, "citation"
    return source


async def _check_country(
    found: CountryFinding, citations: list[Citation], vendor: dict[str, Any], row: LookupRow, ctx: JobContext
) -> Accepted | None:
    if not found.country:
        return None
    code = normalize_country(found.country)
    if code is None:
        row.status, row.reason = "rejected", f"{found.country!r} is not a recognised country"
        return None
    row.value, row.city = code, (found.city or "").strip()[:120] or None
    source = _sourced(found.confidence, found.source_url, citations, row)
    if source is None:
        return None
    evidence = _web(source, "AI headquarters lookup", row.reason)
    conf = Decimal(str(row.confidence or 0))
    items = [NewItem(ProposalItemAction.UPDATE_VENDOR_FIELD, {"field": "hq_country", "value": code},
                     evidence=evidence, confidence=conf)]  # fmt: skip
    if row.city and not vendor["current"].get("city"):
        items.append(NewItem(ProposalItemAction.UPDATE_VENDOR_FIELD, {"field": "hq_city", "value": row.city},
                             evidence=evidence, confidence=conf))  # fmt: skip
    return Accepted(items)


async def _check_description(
    found: DescriptionFinding,
    citations: list[Citation],
    vendor: dict[str, Any],
    row: LookupRow,
    ctx: JobContext,
) -> Accepted | None:
    text = " ".join((found.description or "").split())
    if not text:
        return None
    row.value = text
    low, high = DESCRIPTION_LENGTH
    if not low <= len(text) <= high:
        row.status, row.reason = (
            "rejected",
            f"The description should be {low}-{high} characters (got {len(text)})",
        )
        return None
    source = _sourced(found.confidence, found.source_url, citations, row)
    if source is None:
        return None
    item = NewItem(ProposalItemAction.UPDATE_VENDOR_FIELD, {"field": "description", "value": text},
                   evidence=_web(source, "AI description lookup", text),
                   confidence=Decimal(str(row.confidence or 0)))  # fmt: skip
    return Accepted([item])


class FoundContact(BaseModel):
    contact_type: Literal["general", "sales", "service", "support", "procurement", "other"]
    label: str = Field(description="Which office or department, e.g. 'Head office', 'Spare parts', or ''.")
    email: str | None
    phone: str | None
    country: str | None = Field(description="ISO 3166 alpha-2 code of the office, or null.")


class FoundAddress(BaseModel):
    location_type: Literal["hq", "factory", "service_center", "sales", "rnd", "other"]
    address: str = Field(description="The street address exactly as written on the page.")
    city: str | None
    country: str | None


class ContactsFinding(BaseModel):
    contacts: list[FoundContact]
    addresses: list[FoundAddress]


FIND_CONTACTS = register(
    Prompt(
        name="vendor_contacts_lookup",
        version=1,
        reasoning_effort="low",
        instructions=f"""{_ROLE}
From the company's own web pages given, list how to contact the company and where its offices are.
- contacts: company contact points only: role mailboxes (info@, sales@, service@, enquiries@...) and office or
  department phone lines (switchboard, sales, service hotline). Never a named person's email or mobile number, even if
  the page shows one. One entry per mailbox or number; combine an email and phone of the same office in one entry.
- addresses: head office, factories, service centers, sales offices, R&D, with the street address as written.
- Copy emails, phone numbers and addresses exactly as the page shows them. Never invent or complete them.
{DATA_RULE}""",
        template="Company:\n{company}\n\nPages:\n{pages}",
    )
)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


async def _lookup_contacts(
    vendor: dict[str, Any], ctx: JobContext, call: CallContext
) -> tuple[LookupRow, Accepted | None]:
    """Reads the vendor's own site (home, contact and about pages) and keeps only values that appear on those pages."""
    row = LookupRow(vendor_id=vendor["id"], name=vendor["name"], status="not_found")
    site = vendor["current"].get("website")
    if not site:
        row.status, row.reason = "rejected", "No website to read; find it first"
        return row, None
    base = site if "://" in site else f"https://{site}"
    domain = registrable_domain(base)
    root = base.split("/", 3)[:3]
    home = "/".join(root) + "/"
    starts = [home, *(home + p for p in ("contact", "contact-us", "contacts", "about", "about-us"))]
    async with pipeline.make_fetcher(ctx.settings) as fetcher:
        pages = await Crawler(fetcher, ctx.settings, ctx.sessionmaker).crawl_site(
            starts, max_pages=CONTACT_PAGES, max_depth=1, domain=domain
        )
    pages = [pg for pg in pages if pg.text.strip()]
    if not pages:
        row.status, row.reason = (
            "failed",
            "The website couldn't be read (down, blocked or disallowed by robots.txt)",
        )
        return row, None
    assert ctx.ai is not None
    payload = [{"url": pg.final_url or pg.url, "title": pg.title, "text": pg.text[:8000]} for pg in pages]
    try:
        res = await ctx.ai.run(FIND_CONTACTS, ContactsFinding, call, company=vendor["profile"], pages=payload)
    except BudgetExceeded:
        raise
    except AIError as exc:
        row.status, row.reason = "failed", f"AI lookup failed: {str(exc)[:200]}"
        return row, None

    folded = {pg.final_url or pg.url: " ".join(pg.text.lower().split()) for pg in pages}
    digits = {u: _digits(t) for u, t in folded.items()}

    def page_with(email: str | None, phone: str | None) -> str | None:
        for url, text in folded.items():
            if (email and email.lower() in text) or (
                phone and len(_digits(phone)) >= 6 and _digits(phone) in digits[url]
            ):
                return url
        return None

    items: list[NewItem] = []
    dropped = personal = 0
    for c in res.output.contacts:
        email = c.email if c.email and page_with(c.email, None) else None
        phone = c.phone if c.phone and page_with(None, c.phone) else None
        if not (email or phone):
            dropped += 1
            continue
        try:
            checked = AddContact.model_validate({"contact_type": c.contact_type, "label": c.label, "email": email,
                                                 "phone": phone, "country": c.country})  # fmt: skip
        except ValueError:
            personal += 1  # a person's mailbox (or an unusable value): left out by policy
            continue
        url = page_with(email, phone)
        items.append(NewItem(ProposalItemAction.ADD_CONTACT, checked.model_dump(mode="json"),
                             evidence=_web(url, "Vendor contact page", f"{email or ''} {phone or ''}".strip())))  # fmt: skip
        row.found.append(" · ".join(x for x in (c.label, email, phone) if x))
    for a in res.output.addresses:
        address = " ".join(a.address.split())
        url = next((u for u, t in folded.items() if address and address.lower()[:20] in t), None)
        if url is None:
            dropped += 1
            continue
        items.append(NewItem(ProposalItemAction.ADD_LOCATION,
                             {"location_type": a.location_type, "address": address[:1000], "city": a.city,
                              "country": normalize_country(a.country or "")},
                             evidence=_web(url, "Vendor contact page", address)))  # fmt: skip
        row.found.append(f"{a.location_type.replace('_', ' ')}: {address[:120]}")
    notes = []
    if dropped:
        notes.append(f"{dropped} value(s) not found on the pages were left out")
    if personal:
        notes.append(f"{personal} personal mailbox(es) left out")
    row.reason = "; ".join(notes) or f"Read {len(pages)} page(s) of the vendor's site"
    if not items:
        return row, None
    row.value = f"{sum(1 for i in items if i.action == ProposalItemAction.ADD_CONTACT)} contact(s), " \
                f"{sum(1 for i in items if i.action == ProposalItemAction.ADD_LOCATION)} address(es)"  # fmt: skip
    row.checked, row.source_url = "home_page", pages[0].final_url or pages[0].url
    return row, Accepted(items)


Checker = Callable[[Any, list[Citation], dict[str, Any], LookupRow, JobContext], Awaitable[Accepted | None]]
LOOKUPS: dict[Field_, tuple[Prompt, type[BaseModel], Checker, str]] = {
    "website": (FIND_WEBSITE, WebsiteFinding, _check_website, "Website"),
    "hq_country": (FIND_COUNTRY, CountryFinding, _check_country, "Headquarters"),
    "description": (FIND_DESCRIPTION, DescriptionFinding, _check_description, "Description"),
    # Contacts read the vendor's own pages instead of searching the web (see _lookup_contacts).
    "contacts": (FIND_CONTACTS, ContactsFinding, _check_description, "Contact details"),
}


def already_filled(v: Vendor, field: Field_, with_contacts: set[uuid.UUID] | None = None) -> str | None:
    if field == "contacts":
        return "Has contact details" if with_contacts and v.id in with_contacts else None
    if field == "website":
        return v.website or (v.domains[0].domain if v.domains else None)
    if field == "hq_country":
        return v.hq_country
    return v.description if len(v.description or "") >= DESCRIPTION_LENGTH[0] else None


async def lookup_one(
    field: Field_, vendor: dict[str, Any], ctx: JobContext, call: CallContext
) -> tuple[LookupRow, Accepted | None]:
    if field == "contacts":
        return await _lookup_contacts(vendor, ctx, call)
    row = LookupRow(vendor_id=vendor["id"], name=vendor["name"], status="not_found")
    prompt, schema, check, _ = LOOKUPS[field]
    assert ctx.ai is not None
    try:
        res = await ctx.ai.run(prompt, schema, call, web_search=True, company=vendor["profile"])
    except BudgetExceeded:
        raise
    except AIError as exc:
        row.status, row.reason = "failed", f"AI lookup failed: {str(exc)[:200]}"
        return row, None
    found: Any = res.output
    row.confidence, row.reason = round(found.confidence, 2), found.reason.strip()[:300]
    return row, await check(found, res.citations, vendor, row, ctx)


def _drop_shared_domains(accepted: list[tuple[LookupRow, Accepted]]) -> list[tuple[LookupRow, Accepted]]:
    """One domain found for several vendors in the same run (typically group companies on the parent's site): only
    one vendor may own a domain, so none is proposed and the curator decides."""
    by_domain: dict[str | None, list[LookupRow]] = {}
    for row, _ in accepted:
        by_domain.setdefault(registrable_domain(row.value), []).append(row)
    keep = []
    for row, ok in accepted:
        group = by_domain[registrable_domain(row.value)]
        if len(group) == 1:
            keep.append((row, ok))
            continue
        other = next(r for r in group if r is not row)
        row.status, row.checked = "rejected", None
        row.reason = (
            f"The same website was found for {', '.join(r.name for r in group if r is not row)}. Probably group "
            "companies sharing one site: save it as a group website, or merge them if they are the same company."
        )
        row.other_vendor_id, row.other_vendor_name = other.vendor_id, other.name
    return keep


async def vendor_lookup_handler(ctx: JobContext) -> dict[str, Any]:
    ai = ctx.ai
    if ai is None or not ai.configured:
        raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
    field: Field_ = ctx.input.get("field", "website")
    ids = [uuid.UUID(v) for v in ctx.input["vendor_ids"]][:MAX_VENDORS]
    call = CallContext(ctx.tenant_id, ctx.user_id, ctx.job_id)

    async with ctx.session() as s:
        vendors = list(await s.scalars(select(Vendor).where(Vendor.id.in_(ids))))
        with_contacts = set(
            await s.scalars(
                select(VendorContact.vendor_id).where(VendorContact.vendor_id.in_(ids)).distinct()
            )
        )
        targets: list[dict[str, Any]] = []
        rows: list[LookupRow] = []
        for v in vendors:
            if v.status == VendorStatus.RETIRED:
                continue
            if current := already_filled(v, field, with_contacts):
                rows.append(
                    LookupRow(vendor_id=v.id, name=v.display_name, status="already_filled", value=current)
                )
                continue
            supplies = await s.scalars(
                select(VendorEquipment.name)
                .where(VendorEquipment.vendor_id == v.id, VendorEquipment.status == FactStatus.APPROVED)
                .limit(20)
            )
            profile = {
                "name": v.display_name,
                "legal_name": v.legal_name if v.legal_name != v.display_name else None,
                "also_known_as": [a.alias for a in v.aliases][:5] or None,
                "website": v.website or (v.domains[0].domain if v.domains else None),
                "country": v.hq_country,
                "city": v.hq_city,
                "supplies": sorted(set(supplies))[:10] or None,
                "description": (v.description or "")[:300] or None,
            }
            targets.append({"id": v.id, "name": v.display_name, "current": profile,
                            "profile": {k: x for k, x in profile.items() if x}})  # fmt: skip

    await ctx.progress("searching", 5)
    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0
    accepted: list[tuple[LookupRow, Accepted]] = []

    async def one(vendor: dict[str, Any]) -> None:
        nonlocal done
        async with sem:
            row, ok = await lookup_one(field, vendor, ctx, call)
        rows.append(row)
        if ok:
            accepted.append((row, ok))
        done += 1
        await ctx.progress("searching", 5 + int(85 * done / max(len(targets), 1)))

    await asyncio.gather(*(one(v) for v in targets))
    if field == "website":
        accepted = _drop_shared_domains(accepted)

    await ctx.progress("proposing", 92)
    label = LOOKUPS[field][3]
    async with ctx.session() as s:
        proposals = ProposalService(s, None)
        for row, ok in accepted:
            proposal = await proposals.submit(
                ProposalKind.UPDATE_VENDOR,
                ProposalOrigin.AI_VENDOR_DISCOVERY,
                ok.items,
                title=f"{label} for {row.name}",
                subject_vendor_id=row.vendor_id,
                origin_user_id=ctx.user_id,
                origin_job_id=ctx.job_id,
                confidence=Decimal(str(row.confidence or 0)),
            )
            row.status, row.proposal_id = "proposed", proposal.id
        await s.commit()

    order = {v: i for i, v in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r.vendor_id, len(order)))
    counts = {st: sum(1 for r in rows if r.status == st) for st in STATUSES}
    return {"field": field, "counts": counts, "rows": [r.model_dump(mode="json") for r in rows]}
