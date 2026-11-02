"""Full product details for a vendor: its site served from memory, a scripted AI that also invents a specification
and a product, and the checks that keep only what the pages show. A second run completes the existing product."""

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.crawler.fetcher import Fetcher
from app.discovery import pipeline
from app.discovery.products import ExtractedProduct, ProductCatalog, merge
from app.discovery.schemas import Classification
from tests.conftest import Factory
from tests.test_crawler_unit import DNS, FakeClock, resolve, settings
from tests.test_imports import _drain
from tests.test_search import _vendor

API = "/api/v1"
PRODUCT_PAGE = (
    "<h1>HX-2 between-bearings pump</h1><p>The HX-2 series is an API 610 BB2 between-bearings pump for FPSO water "
    "injection.</p><table><tr><td>Max flow</td><td>2,600 m3/h</td></tr><tr><td>Max head</td><td>300 m</td></tr>"
    "<tr><td>Casing material</td><td>Duplex stainless steel</td></tr></table>"
    "<h2>Field service engineering</h2><p>Our engineers commission and repair pumps on board FPSOs worldwide.</p>"
)


def test_batches_of_one_product_are_merged() -> None:
    def product(specs: list[tuple[str, str]], standards: list[str]) -> ExtractedProduct:
        return ExtractedProduct.model_validate({
            "name": "HX-2 pump", "category_hint": "BB2 pump", "model_series": "HX-2", "overview": None,
            "standards": standards, "applications": [], "confidence": 0.8,
            "specs": [{"name": n, "value": v, "unit": None} for n, v in specs],
            "evidence": [{"url": "https://x.example/p", "quote": "q"}],
        })  # fmt: skip

    [one] = merge(
        [product([("Max flow", "2,600")], ["API 610"]), product([("Max head", "300")], ["ISO 13709"])]
    )
    assert [s.name for s in one.specs] == ["Max flow", "Max head"] and one.standards == [
        "API 610",
        "ISO 13709",
    ]


@pytest.fixture
def tag() -> str:
    return uuid.uuid4().hex[:6]


@pytest.fixture
def site(tag: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    host = f"products-{tag}-maitest.com"
    pages = {"/": "<p>Fjord Flow AS</p><a href='/hx2/'>HX-2 pump</a>", "/hx2/": PRODUCT_PAGE}
    DNS[host] = ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        body = pages.get(request.url.path)
        if request.url.path == "/robots.txt" or body is None:
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=f"<html><body>{body}</body></html>"
        )

    def make(_: Any) -> Fetcher:
        clock = FakeClock()
        return Fetcher(settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                       resolve=resolve, clock=clock, sleep=clock.sleep)  # fmt: skip

    monkeypatch.setattr(pipeline, "make_fetcher", make)
    yield host
    DNS.pop(host, None)


def scripted(host: str, extra_spec: tuple[str, str] | None = None) -> FakeProvider:
    quote = "The HX-2 series is an API 610 BB2 between-bearings pump for FPSO water injection."
    specs: list[dict[str, str | None]] = [
        {"name": "Max flow", "value": "2,600", "unit": "m3/h"},
        {"name": "Max head", "value": "300", "unit": "m"},
        {"name": "Max power", "value": "4,500", "unit": "kW"},
    ]  # 4,500 kW is not on the page
    if extra_spec:
        specs.append({"name": extra_spec[0], "value": extra_spec[1], "unit": None})

    def catalog(_: str) -> dict[str, Any]:
        return {"products": [
            {"name": "HX-2 between-bearings pump", "category_hint": "API 610 BB2 between-bearings pump",
             "model_series": "HX-2", "overview": "Between-bearings pump for FPSO water injection.",
             "standards": ["API 610"], "applications": ["FPSO water injection"], "specs": specs,
             "evidence": [{"url": f"https://{host}/hx2/", "quote": quote}], "confidence": 0.9},
            {"name": "Ghost compressor", "category_hint": "screw compressor", "model_series": "GC-9", "overview": None,
             "standards": [], "applications": [], "specs": [],
             "evidence": [{"url": f"https://{host}/hx2/", "quote": "The GC-9 screw compressor for gas boosting duty"}],
             "confidence": 0.9},
            {"name": "Field service engineering", "category_hint": "field service engineering", "model_series": None,
             "overview": None, "standards": [], "applications": [], "specs": [],
             "evidence": [{"url": f"https://{host}/hx2/",
                           "quote": "Our engineers commission and repair pumps on board FPSOs worldwide."}],
             "confidence": 0.8},
            {"name": "Mystery widget", "category_hint": "quantum flux capacitor", "model_series": "MW-1",
             "overview": None, "standards": [], "applications": [], "specs": [],
             "evidence": [{"url": f"https://{host}/hx2/", "quote": quote}], "confidence": 0.8},
        ]}  # fmt: skip

    return FakeProvider().on(ProductCatalog, catalog).on(Classification, lambda _: {"terms": []})


async def _run(
    client: AsyncClient, container: Container, headers: dict[str, str], vid: uuid.UUID
) -> dict[str, Any]:
    await _drain(container.job_runner)
    resp = await client.post(
        f"{API}/discovery/vendors/{vid}/products", json={"depth": "standard"}, headers=headers
    )
    assert resp.status_code == 202, resp.text
    await _drain(container.job_runner)
    job: dict[str, Any] = (await client.get(f"{API}/jobs/{resp.json()['job_id']}", headers=headers)).json()
    assert job["status"] == "succeeded", job
    return job


async def _approve(client: AsyncClient, headers: dict[str, str], proposal_id: str) -> dict[str, Any]:
    proposal = (await client.get(f"{API}/admin/proposals/{proposal_id}", headers=headers)).json()
    resp = await client.post(f"{API}/admin/proposals/{proposal_id}/approve", json={"version": proposal["version"]},
                             headers=headers)  # fmt: skip
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = proposal
    return data


async def test_product_details(
    client: AsyncClient, factory: Factory, container: Container, site: str, tag: str
) -> None:
    vid = await _vendor(container, f"Fjord Flow {tag}", "NO", [], website=f"https://{site}/")
    curator = await factory.user(None, platform_roles=["curator"])
    previous = container.ai.provider
    try:
        container.ai.provider = scripted(site)
        job = await _run(client, container, curator.headers, vid)
        result = job["result"]
        assert result["pages_read"] == 2 and result["products_found"] == 4 and result["products_kept"] == 2
        assert result["services_kept"] == 1  # field service engineering is a service, not equipment
        assert result["products_unsupported"] == 1 and result["products_unmapped"] == 1
        assert result["specs_unsupported"] == 1  # 4,500 kW isn't on the page
        reasons = {d["name"]: d["reason"] for d in result["not_kept"]}  # nothing found disappears silently
        assert "quote wasn't found" in reasons["Ghost compressor"]
        assert "No matching equipment or service category" in reasons["Mystery widget"]
        proposal = await _approve(client, curator.headers, result["proposal_id"])
        item = next(i for i in proposal["items"] if i["action"] == "add_equipment")
        assert item["payload"]["model_series"] == "HX-2"
        assert any(i["action"] == "add_service" for i in proposal["items"])
        assert item["evidence"] and item["evidence"][0]["url"].endswith("/hx2/")

        vendor = (await client.get(f"{API}/vendors/{vid}", headers=curator.headers)).json()
        [eq] = vendor["equipment"]
        assert eq["specs"]["Max flow"] == "2,600 m3/h" and eq["specs"]["Max head"] == "300 m"
        assert "Max power" not in eq["specs"] and eq["specs"]["Overview"].startswith("Between-bearings")
        assert eq["standards"] == ["API 610"] and eq["applications"] == ["FPSO water injection"]

        # A second run completes the same product instead of duplicating it.
        container.ai.provider = scripted(site, extra_spec=("Casing material", "Duplex stainless steel"))
        job = await _run(client, container, curator.headers, vid)
        await _approve(client, curator.headers, job["result"]["proposal_id"])
        vendor = (await client.get(f"{API}/vendors/{vid}", headers=curator.headers)).json()
        [eq] = vendor["equipment"]
        assert (
            eq["specs"]["Casing material"] == "Duplex stainless steel"
            and eq["specs"]["Max flow"] == "2,600 m3/h"
        )

        no_site = await _vendor(container, f"Siteless {tag}", "NO", [])
        resp = await client.post(
            f"{API}/discovery/vendors/{no_site}/products", json={}, headers=curator.headers
        )
        assert resp.status_code == 422
    finally:
        container.ai.provider = previous
