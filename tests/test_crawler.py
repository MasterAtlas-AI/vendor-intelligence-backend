"""Page cache against the database: stored copies are reused while fresh, revalidated with 304s, and `changed` tracks
whether the text actually changed."""

import uuid
from datetime import timedelta

import httpx
from sqlalchemy import select

from app.core.container import Container
from app.crawler.crawler import Crawler
from app.crawler.models import WebPage
from app.db.session import open_session
from app.domain.principal import TenantContext
from tests.test_crawler_unit import FakeClock, resolve, settings

DNS_HOST = "fjordpumps-maitest.com"


async def test_page_cache_reuse_revalidation_and_change_detection(container: Container) -> None:
    path = f"/p/{uuid.uuid4().hex[:8]}"
    url = f"https://{DNS_HOST}{path}"
    state = {"body": "<p>BB2 pumps</p>", "etag": '"v1"', "requests": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        state["requests"] += 1  # type: ignore[operator]
        if request.headers.get("if-none-match") == state["etag"]:
            return httpx.Response(304)
        return httpx.Response(200, headers={"content-type": "text/html", "etag": str(state["etag"])},
                              text=f"<html><body>{state['body']}</body></html>")  # fmt: skip

    from app.crawler.fetcher import Fetcher

    clock = FakeClock()
    fetcher = Fetcher(settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                      resolve=resolve, clock=clock, sleep=clock.sleep)  # fmt: skip
    crawler = Crawler(fetcher, settings(), container.sessionmaker)

    first = await crawler.page(url)
    assert first.ok and first.changed and not first.from_cache and state["requests"] == 1

    cached = await crawler.page(url)  # fresh copy in storage: no request at all
    assert cached.from_cache and not cached.changed and cached.text == first.text and state["requests"] == 1

    revalidated = await crawler.page(url, max_age=timedelta(0))  # stale: conditional request -> 304
    assert not revalidated.changed and not revalidated.from_cache and state["requests"] == 2

    state["body"], state["etag"] = "<p>BB2 and BB5 pumps</p>", '"v2"'
    updated = await crawler.page(url, max_age=timedelta(0))
    assert updated.changed and "BB5" in updated.text and state["requests"] == 3

    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        row = await s.scalar(select(WebPage).where(WebPage.url == url))
        assert row is not None and row.domain == DNS_HOST and row.etag == '"v2"' and "BB5" in row.text
        assert row.changed_at is not None and row.changed_at >= row.created_at
