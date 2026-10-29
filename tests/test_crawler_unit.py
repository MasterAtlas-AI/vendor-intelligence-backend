"""Crawler without network or database: URL rules, SSRF guard, robots.txt, politeness, limits, HTML/PDF parsing and
site crawling, driven by httpx.MockTransport, a fake DNS resolver and a fake clock."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.crawler.crawler import Crawler, link_score
from app.crawler.fetcher import Fetcher
from app.crawler.html_text import html_to_text
from app.crawler.urls import BlockedURL, check_url, normalize_url, same_site
from tests.pdf_factory import make_pdf

PUBLIC = "93.184.216.34"
DNS = {"fjordpumps-maitest.com": [PUBLIC], "www.fjordpumps-maitest.com": [PUBLIC], "cdn.example.org": [PUBLIC],
       "intranet-maitest.com": ["10.0.0.5"], "mixed-maitest.com": [PUBLIC, "127.0.0.1"]}  # fmt: skip


async def resolve(host: str) -> list[str]:
    if host not in DNS:
        raise OSError("NXDOMAIN")
    return DNS[host]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"crawler_min_delay_seconds": 1.0, "crawler_max_bytes": 50_000}
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


def fetcher(
    handler: Callable[[httpx.Request], httpx.Response], clock: FakeClock | None = None, **cfg: Any
) -> Fetcher:
    clock = clock or FakeClock()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Fetcher(settings(**cfg), client=client, resolve=resolve, clock=clock, sleep=clock.sleep)


def html(body: str, title: str = "") -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                          text=f"<html><head><title>{title}</title></head><body>{body}</body></html>")  # fmt: skip


# ---------------------------------------------------------------- URLs and safety


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "HTTPS://WWW.Example.com:443/a/../b?utm_source=x&b=2&a=1#top",
            "https://www.example.com/a/../b?a=1&b=2",
        ),
        ("http://example.com", "http://example.com/"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("mailto:sales@example.com", None),
        ("javascript:void(0)", None),
        ("https://example.com/logo.PNG", None),
        ("https://example.com/brochure.pdf?gclid=1", "https://example.com/brochure.pdf"),
    ],
)
def test_normalize_url(raw: str, expected: str | None) -> None:
    assert normalize_url(raw) == expected


def test_relative_links_and_same_site() -> None:
    assert (
        normalize_url("../pumps/", "https://fjord.example.com/products/x")
        == "https://fjord.example.com/pumps/"
    )
    assert same_site("https://shop.flowserve.com/x", "flowserve.com")
    assert not same_site("https://flowserve.com.evil.io/x", "flowserve.com")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/",
        "http://localhost:8000/",
        "https://printer.local/",
        "https://intranet-maitest.com/",  # public-looking name, private address
        "https://mixed-maitest.com/",  # one private address is enough to refuse
        "https://user:pass@fjordpumps-maitest.com/",
        "https://fjordpumps-maitest.com:22/",
        "ftp://fjordpumps-maitest.com/",
        "https://does-not-exist-maitest.com/",
    ],
)
async def test_check_url_blocks_non_public_targets(url: str) -> None:
    with pytest.raises(BlockedURL):
        await check_url(url, resolve)


async def test_check_url_allows_public_sites() -> None:
    await check_url("https://fjordpumps-maitest.com/products", resolve)
    await check_url(f"http://{PUBLIC}/", resolve)


# ---------------------------------------------------------------- robots.txt and politeness


async def test_robots_rules_and_crawl_delay_are_obeyed() -> None:
    clock = FakeClock()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\nCrawl-delay: 3\n")
        return html("<p>ok</p>")

    f = fetcher(handler, clock)
    assert (
        await f.fetch("https://fjordpumps-maitest.com/private/prices")
    ).error == "disallowed by robots.txt"
    first = await f.fetch("https://fjordpumps-maitest.com/products")
    second = await f.fetch("https://fjordpumps-maitest.com/services")
    assert first.ok and second.ok
    assert seen == [
        "/robots.txt",
        "/products",
        "/services",
    ]  # robots fetched once; disallowed page never requested
    assert 3.0 in clock.slept  # the site's Crawl-delay spaced the two page requests
    assert f.requests == 3


@pytest.mark.parametrize(("status", "allowed"), [(404, True), (403, True), (503, False)])
async def test_robots_unavailable_vs_unreachable(status: int, allowed: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status) if request.url.path == "/robots.txt" else html("<p>x</p>")

    result = await fetcher(handler).fetch("https://fjordpumps-maitest.com/")
    assert result.ok is allowed


async def test_redirects_are_followed_and_checked_at_every_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "fjordpumps-maitest.com" and request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://www.fjordpumps-maitest.com/new"})
        if request.url.path == "/sneaky":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return html("<p>new home</p>")

    f = fetcher(handler)
    moved = await f.fetch("https://fjordpumps-maitest.com/old")
    assert moved.ok and moved.final_url == "https://www.fjordpumps-maitest.com/new"
    sneaky = await f.fetch("https://fjordpumps-maitest.com/sneaky")
    assert sneaky.status == 0 and sneaky.error is not None and sneaky.error.startswith("blocked")


async def test_size_type_and_conditional_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if path == "/huge":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 60_000)
        if path == "/image":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, headers={"content-type": "text/html", "etag": '"v1"'}, text="<p>hi</p>")

    f = fetcher(handler)
    assert (await f.fetch("https://fjordpumps-maitest.com/huge")).error == "too large"
    assert (await f.fetch("https://fjordpumps-maitest.com/image")).error == "skipped content type image/png"
    first = await f.fetch("https://fjordpumps-maitest.com/page")
    assert first.ok and first.etag == '"v1"'
    again = await f.fetch("https://fjordpumps-maitest.com/page", etag=first.etag)
    assert again.not_modified and again.body == b""


# ---------------------------------------------------------------- parsing and crawling


def test_html_to_text_keeps_content_and_collects_menu_links() -> None:
    doc = html_to_text(
        "<html lang='en'><head><title> Fjord | Pumps </title><meta name='description' content='API 610 &amp; more'>"
        "<script>var tracking = 1</script></head><body><nav><a href='/products'>Products</a></nav>"
        "<div class='cookie-consent'>Accept cookies</div><main><h2>BB2 pumps</h2><p>Between bearings<br>API 610</p>"
        "<p style='display:none'>hidden</p></main><footer>Contact us</footer></body></html>"
    )
    assert doc.title == "Fjord | Pumps" and doc.description == "API 610 & more" and doc.language == "en"
    assert doc.text == "# BB2 pumps\nBetween bearings\nAPI 610"
    assert doc.links == [("/products", "Products")]


def test_link_score_prefers_product_pages() -> None:
    assert link_score("https://x.com/products/pumps", "Pumps") > link_score("https://x.com/news/2026", "News")
    assert link_score("https://x.com/downloads/brochure.pdf", "") > link_score("https://x.com/careers", "")
    assert link_score("https://x.com/de/produkte", "") < link_score("https://x.com/en/products", "")


async def test_crawl_site_visits_best_pages_within_budget() -> None:
    pdf = make_pdf([["Fjord Pumps AS brochure", "API 610 BB2 and BB5 pumps"]])
    site = {
        "/": html("<nav><a href='/news/'>News</a><a href='/careers/'>Jobs</a><a href='/products/'>Products</a>"
                  "<a href='https://other-maitest.com/x'>Partner</a></nav><p>Fjord Pumps</p>", "Home"),
        "/products/": html("<a href='/products/bb2/'>BB2</a><a href='/files/brochure.pdf'>Brochure</a><p>Pumps</p>"),
        "/products/bb2/": html("<p>BB2 between-bearings pump</p>"),
        "/files/brochure.pdf": httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf),
        "/news/": html("<p>news</p>"),
        "/careers/": html("<p>jobs</p>"),
    }  # fmt: skip
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        requested.append(f"{request.url.host}{request.url.path}")
        return site.get(request.url.path, httpx.Response(404))

    crawler = Crawler(fetcher(handler), settings())
    pages = await crawler.crawl_site(["https://fjordpumps-maitest.com/"], max_pages=4, max_depth=2)
    urls = [p.url.removeprefix("https://fjordpumps-maitest.com") for p in pages]
    assert urls == [
        "/",
        "/products/",
        "/files/brochure.pdf",
        "/products/bb2/",
    ]  # products first, news/jobs never
    assert not any("other-maitest.com" in r for r in requested)  # off-site links ignored
    brochure = pages[2]
    assert (
        brochure.is_pdf
        and brochure.page_count == 1
        and "[page 1]" in brochure.text
        and "BB5" in brochure.text
    )
    assert all(p.sha256 and p.changed for p in pages)


def test_www_variant() -> None:
    from app.crawler.crawler import www_variant

    assert www_variant("https://kelvion.com/") == "https://www.kelvion.com/"
    assert www_variant("https://www.kelvion.com/en/") == "https://kelvion.com/en/"
    assert www_variant("https://shop.kelvion.com/") is None


async def test_crawl_site_stops_on_a_site_of_unreadable_pages() -> None:
    links = "".join(f"<a href='/products/p{i}/'>Product {i}</a>" for i in range(100))
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        requested.append(request.url.path)
        if request.url.path == "/":
            return html(f"<nav>{links}</nav><p>Home</p>")
        return html("<script>render()</script>")  # an empty JavaScript shell: no text

    pages = await Crawler(fetcher(handler), settings()).crawl_site(
        ["https://fjordpumps-maitest.com/"], max_pages=5
    )
    assert len(pages) == 1 and len(requested) == 10  # 5 wanted pages x 2 attempts, not all 101 links
