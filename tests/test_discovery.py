"""Vendor discovery end to end with a scripted AI and websites served from memory: request -> job -> proposals with
verified evidence, matched existing vendors, discarded candidates, cost metering, budget and permissions."""

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.ai.ports import Citation, Usage
from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.crawler.fetcher import Fetcher
from app.discovery import pipeline
from app.discovery.schemas import Classification, SearchFindings, SearchPlan, VendorExtraction
from tests.conftest import Factory
from tests.test_crawler_unit import DNS, FakeClock, resolve, settings
from tests.test_imports import _drain, _existing_vendor, _vendor

API = "/api/v1"


def site_pages(tag: str) -> dict[str, dict[str, str]]:
    """host -> path -> html"""
    return {
        f"new-{tag}-maitest.com": {
            "/": "<nav><a href='/products/'>Products</a><a href='/careers/'>Careers</a></nav><p>Fjord Flow AS</p>",
            "/products/": "<h1>Pumps</h1><p>Our HX-2 is an API 610 BB2 between-bearings pump for FPSO water "
                          "injection.</p><p>We hold ISO 9001:2015 certificate no. QA-778.</p>",
            "/careers/": "<p>Jobs</p>",
        },
        f"known-{tag}-maitest.com": {
            "/": "<p>Known Pumps GmbH designs firewater pumps.</p><p>Fire water pumps to NFPA 20 for offshore "
                 "platforms.</p>",
        },
        f"valves-{tag}-maitest.com": {"/": "<p>We distribute industrial valves only.</p>"},
    }  # fmt: skip


@pytest.fixture
def tag() -> str:
    return uuid.uuid4().hex[:6]


@pytest.fixture
def web(tag: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    sites = site_pages(tag)
    for host in sites:
        DNS[host] = ["93.184.216.34"]
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        requested.append(f"{request.url.host}{request.url.path}")
        body = sites.get(request.url.host, {}).get(request.url.path)
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
    yield requested
    for host in sites:
        DNS.pop(host, None)


def scripted_ai(tag: str) -> FakeProvider:
    new, known, valves = (f"https://{h}-{tag}-maitest.com/" for h in ("new", "known", "valves"))
    fake = FakeProvider(usage=Usage(model="fake-model", input_tokens=10_000, output_tokens=1_000),
                        citations=[Citation(f"https://new-{tag}-maitest.com/products/", "Pumps")])  # fmt: skip
    fake.on(SearchPlan, lambda _: {"queries": ["API 610 BB2 pump manufacturer", "FPSO water injection pump OEM",
                                               "firewater pump supplier"]})  # fmt: skip

    def search(text: str) -> dict[str, Any]:
        return {"companies": [
            {"name": "Fjord Flow AS", "website": new, "country": "NO", "kind": "manufacturer",
             "why": "BB2 pumps", "relevance": 0.9},
            {"name": "Known Pumps GmbH", "website": known, "country": "DE", "kind": "manufacturer",
             "why": "Firewater pumps", "relevance": 0.7},
            {"name": "Valve Traders", "website": valves, "country": None, "kind": "manufacturer",
             "why": "maybe", "relevance": 0.5},
            {"name": "LinkedIn", "website": "https://www.linkedin.com/company/x", "country": None,
             "kind": "directory_or_marketplace", "why": "listing", "relevance": 0.6},
        ]}  # fmt: skip

    fake.on(SearchFindings, search)

    def extract(text: str) -> dict[str, Any]:
        base = {"hq_city": None, "services": [], "industries": [], "projects": [], "equipment": [],
                "certifications": []}  # fmt: skip
        if f"new-{tag}" in text:
            return base | {
                "is_relevant": True,
                "relevance_reason": "Makes BB2 pumps",
                "legal_name": "Fjord Flow AS",
                "hq_country": "NO",
                "description": "Norwegian pump maker.",
                "equipment": [
                    {
                        "name": "HX-2",
                        "category_hint": "BB2 pump",
                        "model_series": "HX-2",
                        "standards": ["API 610"],
                        "confidence": 0.9,
                        "evidence": [
                            {
                                "url": f"{new}products/",
                                "quote": "Our HX-2 is an API 610 BB2 between-bearings pump",
                            }
                        ],
                    },
                    {
                        "name": "Invented pump",
                        "category_hint": "BB5 pump",
                        "model_series": None,
                        "standards": [],
                        "confidence": 0.9,
                        "evidence": [
                            {"url": f"{new}products/", "quote": "the BB5 barrel pump range (not on page)"}
                        ],
                    },
                ],
                "certifications": [
                    {
                        "name": "ISO 9001",
                        "certificate_number": "QA-778",
                        "confidence": 0.9,
                        "evidence": [
                            {
                                "url": f"{new}products/",
                                "quote": "We hold ISO 9001:2015 certificate no. QA-778",
                            }
                        ],
                    }
                ],
            }
        if f"known-{tag}" in text:
            return base | {
                "is_relevant": True,
                "relevance_reason": "Firewater pumps",
                "legal_name": "Known Pumps GmbH",
                "hq_country": "DE",
                "description": "German pump maker.",
                "equipment": [
                    {
                        "name": "Firewater pump",
                        "category_hint": "fire water pumps",
                        "model_series": None,
                        "standards": ["NFPA 20"],
                        "confidence": 0.8,
                        "evidence": [
                            {"url": known, "quote": "Fire water pumps to NFPA 20 for offshore platforms"}
                        ],
                    }
                ],
            }
        return base | {"is_relevant": False, "relevance_reason": "Distributes valves, makes no pumps",
                       "legal_name": "Valve Traders", "hq_country": None, "description": ""}  # fmt: skip

    fake.on(VendorExtraction, extract)
    fake.on(Classification, lambda _: {"terms": []})
    return fake


@pytest.fixture
def ai(container: Container, tag: str) -> Iterator[FakeProvider]:
    previous, container.ai.provider = container.ai.provider, scripted_ai(tag)
    yield container.ai.provider  # type: ignore[misc]
    container.ai.provider = previous


REQUEST = {"equipment_categories": ["pumps.centrifugal.api610.bb2"], "industries": ["fpso"],
           "brief": "Water injection pumps for FPSO topsides", "max_candidates": 5, "pages_per_vendor": 3}  # fmt: skip


async def test_discovery_end_to_end(
    client: AsyncClient, factory: Factory, container: Container, ai: FakeProvider, web: list[str], tag: str
) -> None:
    tenant = await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])
    known_id = await _existing_vendor(container, f"Known Pumps {tag} GmbH", f"known-{tag}-maitest.com")

    est = await client.post(f"{API}/discovery/estimate", json=REQUEST, headers=engineer.headers)
    assert est.status_code == 200 and est.json()["queries"] == 3

    await _drain(container.job_runner)
    started = await client.post(f"{API}/discovery/vendors", json=REQUEST, headers=engineer.headers)
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]
    assert await container.job_runner.run_once() == uuid.UUID(job_id)
    job = (await client.get(f"{API}/jobs/{job_id}", headers=engineer.headers)).json()
    assert job["status"] == "succeeded", job
    assert job["result"]["candidates"] == 3 and job["result"]["researched"] == 3
    assert set(job["result"]["stage_started_s"]) >= {
        "planning",
        "searching",
        "researching",
        "proposing",
        "done",
    }
    assert float(job["cost_usd"]) > 0  # every AI call was metered onto the job

    results = (await client.get(f"{API}/discovery/runs/{job_id}/results", headers=engineer.headers)).json()
    by_name = {r["vendor_name"]: r for r in results["items"]}
    assert results["counts"] == {"proposed": 1, "matched": 1, "discarded": 2}

    new = by_name["Fjord Flow AS"]
    assert new["outcome"] == "proposed" and new["details"]["highlights"]["equipment"] == ["HX-2"]
    assert new["details"]["facts"]["unsupported"] == 1  # the invented BB5 pump never reaches a curator
    assert by_name["Valve Traders"]["outcome"] == "discarded"
    assert "Distributes valves" in by_name["Valve Traders"]["messages"][0]
    assert by_name["LinkedIn"]["outcome"] == "discarded"
    new_host = f"new-{tag}-maitest.com"
    visited = [r.removeprefix(new_host) for r in web if r.startswith(new_host)]
    assert visited.index("/products/") < visited.index("/careers/")  # useful pages first, careers last

    curator = await factory.user(None, platform_roles=["curator"])
    proposal = (
        await client.get(f"{API}/admin/proposals/{new['proposal_id']}", headers=curator.headers)
    ).json()
    assert proposal["origin"] == "ai_vendor_discovery" and proposal["status"] == "pending_review"
    actions = {i["action"]: i for i in proposal["items"]}
    assert set(actions) == {"create_vendor", "add_equipment", "add_certification"}
    eq = actions["add_equipment"]
    assert (
        eq["payload"]["category_code"] == "pumps.centrifugal.api610.bb2"
        and eq["payload"]["model_series"] == "HX-2"
    )
    assert (
        eq["evidence"][0]["url"] == f"https://new-{tag}-maitest.com/products/" and eq["confidence"] == "0.90"
    )
    assert actions["add_certification"]["payload"]["certificate_number"] == "QA-778"

    known = (
        by_name[f"Known Pumps {tag} GmbH"]
        if f"Known Pumps {tag} GmbH" in by_name
        else by_name["Known Pumps GmbH"]
    )
    assert known["outcome"] == "matched" and known["vendor_id"] == str(known_id)
    update = (
        await client.get(f"{API}/admin/proposals/{known['proposal_id']}", headers=curator.headers)
    ).json()
    assert update["kind"] == "update_vendor"
    assert any(i["payload"].get("category_code") == "pumps.centrifugal.firewater" for i in update["items"])

    # Discovery finds vendors; it doesn't put them on the organization's own list.
    assert (await _vendor(client, engineer, known_id))["tenant"] is None


async def test_discovery_guards(client: AsyncClient, factory: Factory, container: Container) -> None:
    tenant = await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])
    viewer = await factory.user(tenant, ["viewer"])
    assert (
        await client.post(f"{API}/discovery/vendors", json=REQUEST, headers=viewer.headers)
    ).status_code == 403

    previous, container.ai.provider = container.ai.provider, None
    try:
        resp = await client.post(f"{API}/discovery/vendors", json=REQUEST, headers=engineer.headers)
        assert resp.status_code == 503 and resp.json()["code"] == "ai_not_configured"
    finally:
        container.ai.provider = previous

    container.ai.provider = FakeProvider()
    try:
        bad = await client.post(f"{API}/discovery/vendors", json={**REQUEST, "industries": ["moon_mining"]},
                                headers=engineer.headers)  # fmt: skip
        assert bad.status_code == 422 and bad.json()["errors"][0]["field"] == "industries.0"
        from sqlalchemy import update

        from app.db.session import open_session
        from app.domain.principal import TenantContext
        from app.modules.tenants.models import Tenant

        async with open_session(container.sessionmaker, TenantContext.platform()) as s:
            await s.execute(update(Tenant).where(Tenant.id == tenant).values(quotas={"ai_monthly_usd": 0.01}))
            await s.commit()
        broke = await client.post(f"{API}/discovery/vendors", json=REQUEST, headers=engineer.headers)
        assert broke.status_code == 402 and broke.json()["code"] == "ai_budget_exceeded"
    finally:
        container.ai.provider = previous


async def test_capability_discovery_for_known_vendor(
    client: AsyncClient, factory: Factory, container: Container, web: list[str], tag: str
) -> None:
    from sqlalchemy import select

    from app.db.session import open_session
    from app.domain.enums import ProposalItemAction, ProposalKind, ProposalOrigin
    from app.domain.principal import TenantContext
    from app.modules.proposals.service import NewItem, ProposalService
    from app.modules.vendors.models import FactSource, VendorEquipment

    domain = f"known-{tag}-maitest.com"
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        p = await ProposalService(s, None).submit(
            ProposalKind.NEW_VENDOR,
            ProposalOrigin.MANUAL,
            [
                NewItem(ProposalItemAction.CREATE_VENDOR,
                        {"legal_name": f"Known Pumps {tag} GmbH", "website": domain, "hq_country": "DE"}),
                NewItem(ProposalItemAction.ADD_EQUIPMENT,
                        {"category_code": "pumps.centrifugal.firewater", "name": "Firewater pump"}),
            ],
            auto_approve=True,
        )  # fmt: skip
        await s.commit()
        vendor_id = p.subject_vendor_id

    fake = FakeProvider(usage=Usage(model="fake-model", input_tokens=10_000, output_tokens=1_000))
    known = f"https://{domain}/"
    fake.on(VendorExtraction, lambda text: {
        "is_relevant": True, "relevance_reason": "profile", "legal_name": f"Known Pumps {tag} GmbH",
        "hq_country": "NO", "hq_city": None, "description": "Pump maker.",
        "equipment": [{"name": "Firewater pump", "category_hint": "fire water pumps", "model_series": None,
                       "standards": ["NFPA 20"], "confidence": 0.8,
                       "evidence": [{"url": known, "quote": "Fire water pumps to NFPA 20 for offshore platforms"}]}],
        "certifications": [{"name": "ISO 9001", "certificate_number": None, "confidence": 0.8,
                            "evidence": [{"url": known, "quote": "Known Pumps GmbH designs firewater pumps"}]}],
        "services": [{"name": "Pump repair", "category_hint": "repair", "confidence": 0.9,
                      "evidence": [{"url": known, "quote": "Known Pumps GmbH designs firewater pumps"}]}],
        "industries": [], "projects": [],
    })  # fmt: skip
    fake.on(Classification, lambda _: {"terms": []})
    previous, container.ai.provider = container.ai.provider, fake
    try:
        tenant = await factory.tenant()
        engineer = await factory.user(tenant, ["engineer"])
        await _drain(container.job_runner)
        started = await client.post(f"{API}/discovery/vendors/{vendor_id}/equipment", json={"max_pages": 3},
                                    headers=engineer.headers)  # fmt: skip
        assert started.status_code == 202, started.text
        assert await container.job_runner.run_once() == uuid.UUID(started.json()["job_id"])
        job = (await client.get(f"{API}/jobs/{started.json()['job_id']}", headers=engineer.headers)).json()
        assert job["status"] == "succeeded", job
        r = job["result"]
        assert r["evidence_added_to_existing"] == 1 and r["conflicts"] == 1 and r["outcome"] == "matched"
    finally:
        container.ai.provider = previous

    curator = await factory.user(None, platform_roles=["curator"])
    proposal = (await client.get(f"{API}/admin/proposals/{r['proposal_id']}", headers=curator.headers)).json()
    assert proposal["origin"] == "ai_equipment_discovery" and proposal["kind"] == "update_vendor"
    payloads = [(i["action"], i["payload"]) for i in proposal["items"]]
    assert (
        "update_vendor_field",
        {"field": "hq_country", "value": "NO"},
    ) in payloads  # conflict, old value DE
    assert any(a == "add_certification" for a, _ in payloads)
    assert not any(
        a in ("add_equipment", "add_service") for a, _ in payloads
    )  # known fact; services out of scope

    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        eq_id = await s.scalar(select(VendorEquipment.id).where(VendorEquipment.vendor_id == vendor_id))
        sources = list(await s.scalars(select(FactSource).where(FactSource.fact_id == eq_id)))
        assert len(sources) == 1 and "NFPA 20" in sources[0].snippet

    # Running again doesn't duplicate evidence or re-propose pending facts.
    previous, container.ai.provider = container.ai.provider, fake
    try:
        again = await client.post(f"{API}/discovery/vendors/{vendor_id}/equipment", json={"max_pages": 3},
                                  headers=engineer.headers)  # fmt: skip
        await container.job_runner.run_once()
        r2 = (await client.get(f"{API}/jobs/{again.json()['job_id']}", headers=engineer.headers)).json()[
            "result"
        ]
        assert r2["proposal_id"] is None
    finally:
        container.ai.provider = previous
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        assert len(list(await s.scalars(select(FactSource).where(FactSource.fact_id == eq_id)))) == 1
