"""AI lookups from the data-quality lists, with a scripted AI and sites served from memory: missing websites (every
outcome: proposed, not found, directory, duplicate domain, page that doesn't name the company), headquarters country
and description (cited sources required), and website research for vendors missing facts."""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app.ai.ports import Citation
from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.crawler.fetcher import Fetcher
from app.db.session import open_session
from app.discovery import pipeline
from app.domain.principal import TenantContext
from app.modules.quality.lookup import (
    Accepted,
    CountryFinding,
    DescriptionFinding,
    LookupRow,
    WebsiteFinding,
    _drop_shared_domains,
    name_tokens,
    page_names_company,
)
from app.modules.vendors.models import Vendor
from tests.conftest import Factory
from tests.test_crawler_unit import DNS, FakeClock, resolve, settings
from tests.test_imports import _drain, _existing_vendor
from tests.test_search import _vendor

API = "/api/v1"


def test_page_must_name_the_company() -> None:
    tokens = name_tokens("Fjord Valve Works AS")
    assert tokens[:2] == ["fjord", "valve"]
    assert page_names_company("Welcome to FJORD-VALVE Works, Bergen", tokens)
    assert not page_names_company("Fjord Seafood exports", tokens)
    assert name_tokens("Global Engineering Services Pvt Ltd") == []  # nothing distinctive: never "confirmed"


@pytest.fixture
def tag() -> str:
    return uuid.uuid4().hex[:6]


@pytest.fixture
def web(tag: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    sites = {
        f"fjordvalve-{tag}-maitest.com": f"<title>Fjord Valve Works {tag}</title><p>Ball valves for offshore use.</p>",
        f"wrong-{tag}-maitest.com": "<title>Sunny Holidays</title><p>Book your trip.</p>",
    }
    for host in sites:
        DNS[host] = ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        body = sites.get(request.url.host) if request.url.path == "/" else None
        if body is None:
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=f"<html><body>{body}</body></html>"
        )

    def make(_: Any) -> Fetcher:
        clock = FakeClock()
        return Fetcher(settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                       resolve=resolve, clock=clock, sleep=clock.sleep)  # fmt: skip

    monkeypatch.setattr(pipeline, "make_fetcher", make)
    yield
    for host in sites:
        DNS.pop(host, None)


@pytest.fixture
def ai(container: Container, tag: str) -> Iterator[FakeProvider]:
    answers = {
        "Fjord Valve Works": (f"https://fjordvalve-{tag}-maitest.com/", 0.92),
        "Ghost Pumps": (None, 0.1),
        "Listed Co": ("https://www.linkedin.com/company/listed", 0.9),
        "Copycat Seals": (f"https://taken-{tag}-maitest.com", 0.9),
        "Wrongpage Gear": (f"https://wrong-{tag}-maitest.com", 0.9),
    }

    def find(text: str) -> dict[str, Any]:
        company = json.loads(text.split("<<<DATA\n", 1)[1].split("\nDATA>>>", 1)[0])
        url, confidence = next(v for k, v in answers.items() if company["name"].startswith(k))
        return {"website": url, "confidence": confidence, "reason": "Matched name and products."}

    previous, container.ai.provider = container.ai.provider, FakeProvider().on(WebsiteFinding, find)
    yield container.ai.provider
    container.ai.provider = previous


async def test_find_websites(
    client: AsyncClient, factory: Factory, container: Container, web: None, ai: FakeProvider, tag: str
) -> None:
    names = ["Fjord Valve Works", "Ghost Pumps", "Listed Co", "Copycat Seals", "Wrongpage Gear"]
    ids = {n: await _vendor(container, f"{n} {tag}", "NO", []) for n in names}
    ids["Has Site"] = await _vendor(container, f"Has Site {tag}", "NO", [], website=f"has-{tag}-maitest.com")
    owner = await _existing_vendor(container, f"Owner Seals {tag}", f"taken-{tag}-maitest.com")

    engineer = await factory.user(await factory.tenant(), ["engineer"])
    body = {"vendor_ids": [str(v) for v in ids.values()]}
    assert (await client.post(f"{API}/admin/data-quality/lookup", json=body,
                              headers=engineer.headers)).status_code == 403  # fmt: skip
    curator = await factory.user(None, platform_roles=["curator"])
    await _drain(container.job_runner)
    resp = await client.post(f"{API}/admin/data-quality/lookup", json=body, headers=curator.headers)
    assert resp.status_code == 202, resp.text
    assert resp.json()["vendors"] == 6 and resp.json()["estimated_cost_usd"] > 0
    await _drain(container.job_runner)

    job = (await client.get(f"{API}/jobs/{resp.json()['job_id']}", headers=curator.headers)).json()
    assert job["status"] == "succeeded", job
    rows = {r["vendor_id"]: r for r in job["result"]["rows"]}
    assert [r["vendor_id"] for r in job["result"]["rows"]] == body["vendor_ids"]  # in the order asked
    found = rows[str(ids["Fjord Valve Works"])]
    assert found["status"] == "proposed" and found["checked"] == "home_page" and found["proposal_id"]
    assert found["value"].startswith(f"https://fjordvalve-{tag}-maitest.com")
    assert rows[str(ids["Ghost Pumps"])]["status"] == "not_found"
    assert rows[str(ids["Listed Co"])]["status"] == "rejected"
    copycat = rows[str(ids["Copycat Seals"])]
    assert f"Owner Seals {tag}" in copycat["reason"] and copycat["other_vendor_id"] == str(owner)
    assert "doesn't name the company" in rows[str(ids["Wrongpage Gear"])]["reason"]
    assert rows[str(ids["Has Site"])]["status"] == "already_filled"
    assert job["result"]["counts"]["proposed"] == 1
    assert len(ai.calls) == 5 and all(c.web_search for c in ai.calls)  # the vendor with a site cost nothing

    # The data-quality list shows each vendor's last check, and can leave out vendors already checked.
    board = (await client.get(f"{API}/admin/data-quality", params={"issue": "no_website", "limit": 200},
                              headers=curator.headers)).json()  # fmt: skip
    ghost = next(v for v in board["worst"] if v["vendor_id"] == str(ids["Ghost Pumps"]))
    assert [(c["field"], c["status"]) for c in ghost["ai_checks"]] == [("website", "not_found")]
    hidden = (await client.get(f"{API}/admin/data-quality", headers=curator.headers,
                               params={"issue": "no_website", "limit": 200, "hide_checked": True})).json()  # fmt: skip
    assert str(ids["Ghost Pumps"]) not in {v["vendor_id"] for v in hidden["worst"]}
    searched = (await client.get(f"{API}/admin/data-quality", headers=curator.headers,
                              params={"issue": "no_website", "q": f"ghost pumps {tag}"})).json()  # fmt: skip
    assert [v["vendor_id"] for v in searched["worst"]] == [str(ids["Ghost Pumps"])]

    # The website reaches the vendor only through a curator's approval.
    vendor = (await client.get(f"{API}/vendors/{ids['Fjord Valve Works']}", headers=curator.headers)).json()
    assert not vendor.get("website")
    proposal = (
        await client.get(f"{API}/admin/proposals/{found['proposal_id']}", headers=curator.headers)
    ).json()
    approved = await client.post(f"{API}/admin/proposals/{found['proposal_id']}/approve",
                                 json={"version": proposal["version"]}, headers=curator.headers)  # fmt: skip
    assert approved.status_code == 200, approved.text
    vendor = (await client.get(f"{API}/vendors/{ids['Fjord Valve Works']}", headers=curator.headers)).json()
    assert vendor["website"].startswith(f"https://fjordvalve-{tag}-maitest.com")

    # The clash names the owner; a group company can still get the site without taking the domain.
    copy_id = ids["Copycat Seals"]
    website = f"https://taken-{tag}-maitest.com/seals"

    async def propose(shared: bool) -> str:
        payload: dict[str, Any] = {"field": "website", "value": website}
        if shared:
            payload["shared_domain"] = True
        created = await client.post(f"{API}/admin/proposals", headers=curator.headers, json={
            "kind": "update_vendor", "subject_vendor_id": str(copy_id),
            "items": [{"action": "update_vendor_field", "payload": payload}],
        })  # fmt: skip
        assert created.status_code == 201, created.text
        pid: str = created.json()["id"]
        return pid

    pid = await propose(shared=False)
    clash = await client.post(
        f"{API}/admin/proposals/{pid}/approve", json={"version": 1}, headers=curator.headers
    )
    assert clash.status_code == 409 and clash.json()["code"] == "duplicate_vendor"
    assert f"Owner Seals {tag}" in clash.json()["detail"] and str(owner) not in clash.json()["detail"]
    assert clash.json()["errors"][0]["other_vendor_id"] == str(owner)
    pid = await propose(shared=True)
    shared = await client.post(
        f"{API}/admin/proposals/{pid}/approve", json={"version": 1}, headers=curator.headers
    )
    assert shared.status_code == 200, shared.text
    vendor = (await client.get(f"{API}/vendors/{copy_id}", headers=curator.headers)).json()
    assert vendor["website"] == website
    owner_vendor = (await client.get(f"{API}/vendors/{owner}", headers=curator.headers)).json()
    assert f"taken-{tag}-maitest.com" in str(owner_vendor)  # the domain stays with its owner

    too_many = {"vendor_ids": [str(uuid.uuid4()) for _ in range(26)]}
    assert (await client.post(f"{API}/admin/data-quality/lookup", json=too_many,
                              headers=curator.headers)).status_code == 422  # fmt: skip


def _company(text: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(text.split("<<<DATA\n", 1)[1].split("\nDATA>>>", 1)[0])
    return data


@pytest.fixture
def ai_basics(container: Container, tag: str) -> Iterator[FakeProvider]:
    def country(text: str) -> dict[str, Any]:
        name = _company(text)["name"]
        if name.startswith("Oslo Seal"):
            return {"country": "Norway", "city": "Oslo", "source_url": f"https://oslo-{tag}-maitest.com/contact",
                    "confidence": 0.9, "reason": "The contact page gives a head office in Oslo."}  # fmt: skip
        if name.startswith("Unsure Gear"):
            return {
                "country": "DE",
                "city": None,
                "source_url": None,
                "confidence": 0.5,
                "reason": "Maybe Germany.",
            }
        return {
            "country": "Atlantis",
            "city": None,
            "source_url": None,
            "confidence": 0.9,
            "reason": "Legend.",
        }

    def describe(text: str) -> dict[str, Any]:
        if _company(text)["name"].startswith("Oslo Seal"):
            return {"description": "Oslo Seal Works makes mechanical seals for centrifugal pumps used offshore.",
                    "source_url": f"https://oslo-{tag}-maitest.com/about", "confidence": 0.85,
                    "reason": "From the company's about page."}  # fmt: skip
        return {"description": "Great.", "source_url": None, "confidence": 0.9, "reason": "Too short."}

    fake = FakeProvider(citations=[Citation(f"https://oslo-{tag}-maitest.com/contact", "Contact")])
    fake.on(CountryFinding, country).on(DescriptionFinding, describe)
    previous, container.ai.provider = container.ai.provider, fake
    yield fake
    container.ai.provider = previous


async def _run_lookup(client: AsyncClient, container: Container, headers: dict[str, str], ids: list[str],
                      field: str) -> dict[str, Any]:  # fmt: skip
    await _drain(container.job_runner)
    resp = await client.post(f"{API}/admin/data-quality/lookup", json={"vendor_ids": ids, "field": field},
                             headers=headers)  # fmt: skip
    assert resp.status_code == 202, resp.text
    await _drain(container.job_runner)
    job: dict[str, Any] = (await client.get(f"{API}/jobs/{resp.json()['job_id']}", headers=headers)).json()
    assert job["status"] == "succeeded", job
    return job


async def test_country_and_description_lookup(
    client: AsyncClient, factory: Factory, container: Container, ai_basics: FakeProvider, tag: str
) -> None:
    oslo = await _vendor(container, f"Oslo Seal Works {tag}", "NO", [])
    unsure = await _vendor(container, f"Unsure Gear {tag}", "NO", [])
    myth = await _vendor(container, f"Myth Pumps {tag}", "NO", [])
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        await s.execute(update(Vendor).where(Vendor.id.in_([oslo, unsure, myth])).values(hq_country=None))
        await s.commit()
    curator = await factory.user(None, platform_roles=["curator"])
    ids = [str(oslo), str(unsure), str(myth)]

    job = await _run_lookup(client, container, curator.headers, ids, "hq_country")
    rows = {r["vendor_id"]: r for r in job["result"]["rows"]}
    assert job["result"]["field"] == "hq_country"
    assert rows[str(oslo)]["status"] == "proposed" and rows[str(oslo)]["value"] == "NO"
    assert rows[str(oslo)]["city"] == "Oslo" and rows[str(oslo)]["source_url"].endswith("/contact")
    assert rows[str(unsure)]["status"] == "rejected" and "Not sure enough" in rows[str(unsure)]["reason"]
    assert rows[str(myth)]["status"] == "rejected" and "not a recognised country" in rows[str(myth)]["reason"]
    proposal = (await client.get(f"{API}/admin/proposals/{rows[str(oslo)]['proposal_id']}",
                                 headers=curator.headers)).json()  # fmt: skip
    fields = {i["payload"]["field"]: i["payload"]["value"] for i in proposal["items"]}
    assert fields == {"hq_country": "NO", "hq_city": "Oslo"}

    job = await _run_lookup(client, container, curator.headers, ids[:2], "description")
    rows = {r["vendor_id"]: r for r in job["result"]["rows"]}
    assert rows[str(oslo)]["status"] == "proposed" and "mechanical seals" in rows[str(oslo)]["value"]
    assert rows[str(unsure)]["status"] == "rejected" and "40-600 characters" in rows[str(unsure)]["reason"]
    bad_field = await client.post(f"{API}/admin/data-quality/lookup", json={"vendor_ids": ids, "field": "plan"},
                                  headers=curator.headers)  # fmt: skip
    assert bad_field.status_code == 422


async def test_research_skips_vendors_without_a_website(
    client: AsyncClient, factory: Factory, container: Container, tag: str
) -> None:
    with_site = await _vendor(container, f"Sited Pumps {tag}", "NO", [], website=f"sited-{tag}-maitest.com")
    no_site = await _vendor(container, f"Siteless Pumps {tag}", "NO", [])
    previous, container.ai.provider = container.ai.provider, FakeProvider()
    try:
        curator = await factory.user(None, platform_roles=["curator"])
        body = {"vendor_ids": [str(with_site), str(no_site)]}
        resp = await client.post(f"{API}/admin/data-quality/research", json=body, headers=curator.headers)
        assert resp.status_code == 202, resp.text
        out = {v["vendor_id"]: v for v in resp.json()["vendors"]}
        assert resp.json()["queued"] == 1 and out[str(with_site)]["job_id"]
        assert "No website" in out[str(no_site)]["skipped"]
        job = (
            await client.get(f"{API}/jobs/{out[str(with_site)]['job_id']}", headers=curator.headers)
        ).json()
        assert job["type"] == "enrichment" and job["input"]["vendor_id"] == str(with_site)
        again = (
            await client.post(f"{API}/admin/data-quality/research", json=body, headers=curator.headers)
        ).json()
        assert again["queued"] == 0  # already queued: never paid for twice
        assert {v["skipped"] for v in again["vendors"]} == {
            "Already being researched",
            "No website to read; find it first",
        }
    finally:
        container.ai.provider = previous


def test_one_domain_for_two_vendors_proposes_neither() -> None:
    def row(name: str, url: str) -> tuple[LookupRow, Accepted]:
        return LookupRow(vendor_id=uuid.uuid4(), name=name, status="not_found", value=url), Accepted([])

    rows = [row("Wilhelmsen Ship Management", "https://www.wilhelmsen.com/"),
            row("Wilhelmsen Ships Service Inc", "https://wilhelmsen.com/ships-service"),
            row("Fjord Valve Works", "https://fjordvalve.no/")]  # fmt: skip
    kept = _drop_shared_domains(rows)
    assert [r.name for r, _ in kept] == ["Fjord Valve Works"]
    management, service = rows[0][0], rows[1][0]
    assert management.status == "rejected" and "Wilhelmsen Ships Service Inc" in management.reason
    assert service.other_vendor_id == management.vendor_id
