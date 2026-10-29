"""Page cache + site crawl on top of the polite fetcher.

`Crawler.page(url)` returns a parsed page, reusing the stored copy while it is fresh (`crawler_cache_days`) and
re-validating it with a conditional request after that. `Page.changed` says whether the text differs from the last
stored copy, so pipelines can skip unchanged pages.

`Crawler.crawl_site(...)` walks one vendor's site breadth-first within a page and depth budget, visiting the most
promising links first (products, equipment, services, certifications, projects, brochures).
"""

import hashlib
import heapq
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import undefer

from app.ai.guards import sanitise
from app.core.config import Settings
from app.core.exceptions import ValidationFailed
from app.crawler.fetcher import Fetcher, FetchResult
from app.crawler.html_text import html_to_text
from app.crawler.models import WebPage
from app.crawler.urls import normalize_url, same_site
from app.db.session import open_session
from app.domain.normalization import registrable_domain
from app.domain.principal import TenantContext
from app.modules.imports.pdf_extract import read_pdf

MAX_TEXT_CHARS = 300_000
MAX_LINKS = 400
ATTEMPTS_PER_PAGE = 2  # fetches allowed per wanted page (start pages and the www retry included)

GOOD_WORDS = re.compile(
    r"product|equipment|solution|service|capabilit|portfolio|range|catalog|brochure|datasheet|download|"
    r"certif|quality|approval|iso|api|asme|project|reference|case.?stud|industr|application|about|company|"
    r"pump|valve|compressor|turbine|exchanger|vessel|skid|package|system",
    re.I,
)
BAD_WORDS = re.compile(
    r"news|blog|press|event|career|job|vacanc|privacy|cookie|terms|legal|imprint|impressum|login|signin|"
    r"register|cart|basket|checkout|search|tag/|author/|feed|wp-json|calendar|webinar|investor|sitemap",
    re.I,
)
LANG_PREFIX = re.compile(r"^/(?!en(?:[-_][a-z]{2})?/)[a-z]{2}(?:[-_][a-z]{2})?/", re.I)


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    content_type: str = ""
    title: str = ""
    description: str = ""
    language: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    page_count: int | None = None
    sha256: str | None = None
    fetched_at: datetime | None = None
    changed: bool = True  # text differs from the previously stored copy (or there was none)
    from_cache: bool = False  # served from storage without a request
    error: str | None = None
    pdf_bytes: bytes | None = None
    suspicious_lines: int = 0  # lines removed because they read like instructions to an AI

    @property
    def suspicious(self) -> bool:
        return self.suspicious_lines > 0

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text)

    @property
    def is_pdf(self) -> bool:
        return self.content_type == "application/pdf"


def link_score(url: str, anchor: str) -> int:
    path = urlsplit(url).path
    score = 3 * len(GOOD_WORDS.findall(f"{path} {anchor}")) - 4 * len(BAD_WORDS.findall(path))
    if path.lower().endswith(".pdf"):
        score += 2
    if LANG_PREFIX.match(path):
        score -= 6  # another language edition of the same content
    return score - path.count("/")


def www_variant(url: str) -> str | None:
    """https://example.com/x -> https://www.example.com/x and back; None for other subdomains."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host.startswith("www."):
        other = host[4:]
    elif host == registrable_domain(url):
        other = f"www.{host}"
    else:
        return None
    return normalize_url(parts._replace(netloc=other).geturl())


def focus_key(term: str) -> str:
    """'API 610' -> 'api610', 'water injection' -> 'waterinjection' (compared against folded URL + anchor text)."""
    return re.sub(r"[^a-z0-9]", "", term.lower())


def focus_bonus(url: str, anchor: str, keys: list[str]) -> int:
    folded = re.sub(r"[^a-z0-9]", "", f"{url} {anchor}".lower())
    return 8 * sum(1 for k in keys if len(k) >= 3 and k in folded)


def parse(result: FetchResult, max_pdf_pages: int) -> Page:
    """Turns a fetched response into text and same-document links."""
    page = Page(result.url, result.final_url, result.status, result.content_type, error=result.error,
                fetched_at=datetime.now(UTC))  # fmt: skip
    if not result.ok:
        return page
    if result.is_pdf:
        page.content_type = "application/pdf"
        try:
            pdf = read_pdf(result.body, max_pages=max_pdf_pages)
        except ValidationFailed as exc:
            page.error = exc.detail or "unreadable PDF"
            return page
        page.text = "\n\n".join(f"[page {i}]\n{t.strip()}" for i, t in enumerate(pdf.pages, 1) if t.strip())
        page.page_count = pdf.page_count
        page.pdf_bytes = result.body
        if pdf.scanned:
            page.error = "scanned PDF without a text layer"
    elif result.content_type == "text/plain":
        page.text = result.body.decode("utf-8", errors="replace")
    else:
        doc = html_to_text(result.body.decode("utf-8", errors="replace"))
        page.title, page.description, page.language, page.text = (
            doc.title,
            doc.description,
            doc.language,
            doc.text,
        )
        seen: set[str] = set()
        for href, anchor in doc.links:
            if (absolute := normalize_url(href, result.final_url)) and absolute not in seen:
                seen.add(absolute)
                page.links.append((absolute, anchor))
                if len(page.links) >= MAX_LINKS:
                    break
    clean(page)
    page.text = page.text[:MAX_TEXT_CHARS]
    page.sha256 = hashlib.sha256(page.text.encode()).hexdigest() if page.text else None
    return page


def clean(page: Page) -> Page:
    """Strips invisible characters and instruction-like lines from what the AI will read (see app.ai.guards).
    Idempotent, so it also applies to copies stored before it existed."""
    text = sanitise(page.text)
    page.text, page.suspicious_lines = text.text, text.redacted_lines
    page.title = sanitise(page.title).text
    page.description = sanitise(page.description).text
    return page


def _from_row(row: WebPage, *, changed: bool, from_cache: bool) -> Page:
    return clean(Page(row.url, row.final_url, row.status, row.content_type, row.title, row.description, row.language,
                row.text, [(u, a) for u, a in row.links], row.page_count, row.sha256, row.fetched_at,
                changed=changed, from_cache=from_cache, error=row.error))  # fmt: skip


class Crawler:
    def __init__(
        self,
        fetcher: Fetcher,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.s = settings
        self.sessionmaker = sessionmaker  # None: no cache (tests, one-off use)

    # ------------------------------------------------------------------ storage

    async def _stored(self, url: str) -> WebPage | None:
        if self.sessionmaker is None:
            return None
        async with open_session(self.sessionmaker, TenantContext.platform()) as s:
            return await s.scalar(select(WebPage).where(WebPage.url == url))

    async def _store(self, page: Page, result: FetchResult, previous: WebPage | None) -> None:
        if self.sessionmaker is None:
            return
        now = datetime.now(UTC)
        async with open_session(self.sessionmaker, TenantContext.platform()) as s:
            row = await s.scalar(select(WebPage).where(WebPage.url == page.url).options(undefer(WebPage.raw)))
            if row is None:
                row = WebPage(url=page.url, links=[], text="")
                s.add(row)
            values: dict[str, Any] = {
                "final_url": page.final_url, "domain": registrable_domain(page.final_url), "status": page.status,
                "content_type": page.content_type[:100], "title": page.title[:300],
                "description": page.description[:500], "language": page.language[:12], "text": page.text,
                "links": [[u, a] for u, a in page.links], "page_count": page.page_count, "sha256": page.sha256,
                "etag": (result.etag or "")[:200] or None, "last_modified": (result.last_modified or "")[:100] or None,
                "error": (page.error or "")[:300] or None, "fetched_at": now,
                "changed_at": now if page.changed else (previous.changed_at if previous else now),
                "raw": page.pdf_bytes,
            }  # fmt: skip
            for key, value in values.items():
                setattr(row, key, value)
            await s.commit()

    async def _touch(self, url: str) -> None:
        if self.sessionmaker is None:
            return
        async with open_session(self.sessionmaker, TenantContext.platform()) as s:
            row = await s.scalar(select(WebPage).where(WebPage.url == url))
            if row is not None:
                row.fetched_at = datetime.now(UTC)
                await s.commit()

    # ------------------------------------------------------------------ pages

    async def page(self, url: str, *, max_age: timedelta | None = None) -> Page:
        normalized = normalize_url(url)
        if normalized is None:
            return Page(url, url, 0, error="unsupported URL")
        max_age = max_age if max_age is not None else timedelta(days=self.s.crawler_cache_days)
        previous = await self._stored(normalized)
        if previous and previous.error is None and datetime.now(UTC) - previous.fetched_at < max_age:
            return _from_row(previous, changed=False, from_cache=True)

        result = await self.fetcher.fetch(
            normalized,
            etag=previous.etag if previous else None,
            last_modified=previous.last_modified if previous else None,
        )
        if result.not_modified and previous is not None:
            await self._touch(normalized)
            return _from_row(previous, changed=False, from_cache=False)
        max_pdf_pages = self.s.pdf_max_pages
        page = await anyio.to_thread.run_sync(lambda: parse(result, max_pdf_pages))
        page.url = normalized
        page.changed = previous is None or previous.sha256 != page.sha256
        if result.status:  # a response came back (even an error page): remember it
            await self._store(page, result, previous)
        return page

    async def crawl_site(
        self,
        start_urls: list[str],
        *,
        max_pages: int = 30,
        max_depth: int = 2,
        domain: str | None = None,
        focus: list[str] | None = None,
    ) -> list[Page]:
        """Pages of one site, most useful first. Links leaving the site (other registrable domains) are ignored.
        `focus` terms (e.g. "BB2", "API 610", "water injection") pull matching links ahead of everything else."""
        focus_keys = [k for k in (focus_key(t) for t in focus or []) if k]
        starts = [u for u in (normalize_url(x) for x in start_urls) if u]
        if not starts:
            return []
        domain = domain or registrable_domain(starts[0])
        if domain is None:
            return []
        # Best-first rather than breadth-first: a strict level-by-level crawl spends the page budget on shallow
        # news/careers pages before reaching product pages. Priority = relevance, with a small cost per level.
        queue: list[tuple[int, int, int, str]] = []  # (priority, order, depth, url); lower priority = sooner
        seen: set[str] = set()
        order = 0
        for url in starts:
            seen.add(url)
            heapq.heappush(queue, (-100, order, 0, url))  # start pages always first
            order += 1
        pages: list[Page] = []
        # Unreadable pages (errors, empty JavaScript shells) don't count towards max_pages, so cap attempts too: one
        # such site once took 13 minutes walking its whole link list at the polite rate.
        attempts = 0
        while queue and len(pages) < max_pages and attempts < max_pages * ATTEMPTS_PER_PAGE:
            _, _, depth, url = heapq.heappop(queue)
            attempts += 1
            page = await self.page(url)
            if page.final_url != url and (final := normalize_url(page.final_url)):
                seen.add(final)
            if page.ok:
                pages.append(page)
            elif depth == 0 and (other := www_variant(url)) and other not in seen:
                # Many sites only work on one of example.com / www.example.com (bad certificate, no robots.txt on
                # the other), and stored websites are often written without "www".
                seen.add(other)
                heapq.heappush(queue, (-100, order, 0, other))
                order += 1
            if depth >= max_depth:
                continue
            for link, anchor in page.links:
                if link in seen or not same_site(link, domain):
                    continue
                seen.add(link)
                priority = 2 * (depth + 1) - link_score(link, anchor) - focus_bonus(link, anchor, focus_keys)
                heapq.heappush(queue, (priority, order, depth + 1, link))
                order += 1
        return pages
