"""A polite HTTP fetcher for vendor websites.

- robots.txt is obeyed (RFC 9309): 4xx means "no rules", 5xx or unreachable means "don't crawl" for a while.
- One request at a time per site, at least `crawler_min_delay_seconds` apart, or the site's Crawl-delay (capped).
- Every URL, including each redirect hop, passes the SSRF check in `urls.check_url`.
- Responses are capped at `crawler_max_bytes`; only HTML, plain text and PDF are kept.
- Conditional requests (ETag / Last-Modified) let unchanged pages come back as 304 without a download.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.crawler.urls import BlockedURL, Resolver, check_url, system_resolver

log = get_logger(__name__)

ROBOTS_AGENT = "MasterAtlasBot"
MAX_REDIRECTS = 5
ROBOTS_TTL_SECONDS = 3600
ROBOTS_FAILURE_TTL_SECONDS = 600
KEEP_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/pdf")


@dataclass
class FetchResult:
    url: str  # as requested
    final_url: str  # after redirects
    status: int  # HTTP status; 0 when no request was made (blocked, disallowed) or it failed
    content_type: str = ""
    body: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def is_pdf(self) -> bool:
        return self.content_type == "application/pdf" or self.body[:5] == b"%PDF-"


@dataclass
class _Robots:
    parser: RobotFileParser | None  # None = everything allowed
    deny_all: bool
    expires: float
    crawl_delay: float | None = None


@dataclass
class _Host:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_at: float = 0.0


class Fetcher:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        resolve: Resolver = system_resolver,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.s = settings
        self.client = client or httpx.AsyncClient(
            timeout=settings.crawler_timeout_seconds,
            follow_redirects=False,  # redirects are followed by hand so each hop is checked
            headers={"User-Agent": settings.crawler_user_agent, "Accept-Language": "en;q=1.0, *;q=0.5"},
        )
        self.resolve = resolve
        self.clock = clock
        self.sleep = sleep
        self._robots: dict[str, _Robots] = {}
        self._hosts: dict[str, _Host] = {}
        self.requests = 0  # made over the network, robots.txt included

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ politeness

    async def _turn(self, origin: str, delay: float) -> None:
        """Waits until this origin may be requested again, then books the next slot."""
        host = self._hosts.setdefault(origin, _Host())
        async with host.lock:
            wait = host.next_at - self.clock()
            if wait > 0:
                await self.sleep(wait)
            host.next_at = self.clock() + delay

    def _delay(self, origin: str) -> float:
        robots = self._robots.get(origin)
        site = robots.crawl_delay if robots and robots.crawl_delay else 0.0
        return max(self.s.crawler_min_delay_seconds, min(site, self.s.crawler_max_crawl_delay_seconds))

    async def _robots_for(self, origin: str) -> _Robots:
        cached = self._robots.get(origin)
        if cached and cached.expires > self.clock():
            return cached
        url = f"{origin}/robots.txt"
        await self._turn(origin, self.s.crawler_min_delay_seconds)
        try:
            for _ in range(MAX_REDIRECTS + 1):  # redirects by hand: every hop passes the SSRF check
                await check_url(url, self.resolve)
                self.requests += 1
                resp = await self.client.get(url)
                if not (resp.is_redirect and (location := resp.headers.get("location"))):
                    break
                url = str(resp.url.join(location))
            else:
                raise BlockedURL("too many redirects for robots.txt")
        except (httpx.HTTPError, BlockedURL) as exc:
            log.info("crawler.robots_unreachable", origin=origin, error=str(exc)[:200])
            robots = _Robots(None, True, self.clock() + ROBOTS_FAILURE_TTL_SECONDS)
        else:
            if resp.status_code >= 500:
                robots = _Robots(None, True, self.clock() + ROBOTS_FAILURE_TTL_SECONDS)
            elif resp.status_code >= 400:
                robots = _Robots(None, False, self.clock() + ROBOTS_TTL_SECONDS)
            else:
                parser = RobotFileParser()
                parser.parse(resp.text[:500_000].splitlines())
                delay = parser.crawl_delay(ROBOTS_AGENT)
                robots = _Robots(parser, False, self.clock() + ROBOTS_TTL_SECONDS,
                                 float(delay) if delay is not None else None)  # fmt: skip
        self._robots[origin] = robots
        return robots

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        robots = await self._robots_for(f"{parts.scheme}://{parts.netloc}")
        if robots.deny_all:
            return False
        return robots.parser is None or robots.parser.can_fetch(ROBOTS_AGENT, url)

    # ------------------------------------------------------------------ fetching

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                await check_url(current, self.resolve)
            except BlockedURL as exc:
                return FetchResult(url, current, 0, error=f"blocked: {exc}")
            if not await self.allowed(current):
                return FetchResult(url, current, 0, error="disallowed by robots.txt")
            parts = urlsplit(current)
            origin = f"{parts.scheme}://{parts.netloc}"
            await self._turn(origin, self._delay(origin))
            headers = {}
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
            try:
                self.requests += 1
                async with self.client.stream("GET", current, headers=headers) as resp:
                    if resp.is_redirect and (location := resp.headers.get("location")):
                        current = str(resp.url.join(location))
                        etag = last_modified = None  # validators belong to the original URL
                        continue
                    return await self._read(url, current, resp)
            except httpx.HTTPError as exc:
                return FetchResult(url, current, 0, error=f"{type(exc).__name__}: {exc}"[:300])
        return FetchResult(url, current, 0, error="too many redirects")

    async def _read(self, url: str, final_url: str, resp: httpx.Response) -> FetchResult:
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        result = FetchResult(url, final_url, resp.status_code, ctype,
                             etag=resp.headers.get("etag"), last_modified=resp.headers.get("last-modified"))  # fmt: skip
        if resp.status_code == 304:
            return result
        if resp.status_code >= 400:
            result.error = f"HTTP {resp.status_code}"
            return result
        if ctype and ctype not in KEEP_TYPES:
            result.error = f"skipped content type {ctype}"
            return result
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.s.crawler_max_bytes:
            result.error = "too large"
            return result
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) > self.s.crawler_max_bytes:
                result.error = "too large"
                return result
        result.body = bytes(body)
        return result
