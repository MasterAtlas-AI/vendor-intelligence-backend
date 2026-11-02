"""Company contact details from the vendor's own pages: role mailboxes and phone lines only (no named people), every
value must appear on a page we read, and they reach the vendor only through approval."""

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
from app.modules.proposals.payloads import AddContact, company_mailbox
from app.modules.quality.lookup import ContactsFinding
from tests.conftest import Factory
from tests.test_crawler_unit import DNS, FakeClock, resolve, settings
from tests.test_imports import _drain
from tests.test_search import _vendor

API = "/api/v1"


def test_only_company_mailboxes() -> None:
    assert (
        company_mailbox("sales@acme.com")
        and company_mailbox("enquiries.sg@acme.com")
        and company_mailbox("kl@acme.com")
    )
    assert not company_mailbox("john.smith@acme.com") and not company_mailbox("rajesh@acme.com")
    with pytest.raises(ValueError):
        AddContact.model_validate({"email": "john.smith@acme.com"})
    with pytest.raises(ValueError):
        AddContact.model_validate({"label": "Nothing to reach"})
    assert AddContact.model_validate({"phone": "+60 3-2161 1234 ext."}).phone == "+60 3-2161 1234"


@pytest.fixture
def tag() -> str:
    return uuid.uuid4().hex[:6]


@pytest.fixture
def site(tag: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    host = f"contacts-{tag}-maitest.com"
    pages = {
        "/": "<p>Fjord Seals AS makes mechanical seals.</p><a href='/contact'>Contact</a>",
        "/contact": "<h1>Contact us</h1><p>Head office: Strandgata 12, 5013 Bergen, Norway</p>"
                    "<p>General enquiries: info@fjordseals.example, +47 55 12 34 56</p>"
                    "<p>Spare parts: parts@fjordseals.example</p><p>Our CEO: john.smith@fjordseals.example</p>",
    }  # fmt: skip
    DNS[host] = ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        body = pages.get(request.url.path)
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
    yield host
    DNS.pop(host, None)


def scripted() -> FakeProvider:
    def contacts(_: str) -> dict[str, Any]:
        return {
            "contacts": [
                {"contact_type": "general", "label": "Head office", "email": "info@fjordseals.example",
                 "phone": "+47 55 12 34 56", "country": "NO"},
                {"contact_type": "sales", "label": "Spare parts", "email": "parts@fjordseals.example", "phone": None,
                 "country": "NO"},
                {"contact_type": "other", "label": "CEO", "email": "john.smith@fjordseals.example", "phone": None,
                 "country": "NO"},
                {"contact_type": "service", "label": "Hotline", "email": None, "phone": "+47 99 88 77 66",
                 "country": "NO"},  # invented: not on the page
            ],
            "addresses": [{"location_type": "hq", "address": "Strandgata 12, 5013 Bergen, Norway", "city": "Bergen",
                           "country": "NO"}],
        }  # fmt: skip

    return FakeProvider().on(ContactsFinding, contacts)


async def test_contact_details(
    client: AsyncClient, factory: Factory, container: Container, site: str, tag: str
) -> None:
    vid = await _vendor(container, f"Fjord Seals {tag}", "NO", [], website=f"https://{site}/")
    no_site = await _vendor(container, f"Siteless Seals {tag}", "NO", [])
    curator = await factory.user(None, platform_roles=["curator"])
    previous, container.ai.provider = container.ai.provider, scripted()
    try:
        await _drain(container.job_runner)
        resp = await client.post(f"{API}/admin/data-quality/lookup", headers=curator.headers,
                                 json={"vendor_ids": [str(vid), str(no_site)], "field": "contacts"})  # fmt: skip
        assert resp.status_code == 202, resp.text
        await _drain(container.job_runner)
        job = (await client.get(f"{API}/jobs/{resp.json()['job_id']}", headers=curator.headers)).json()
        assert job["status"] == "succeeded", job
    finally:
        container.ai.provider = previous
    rows = {r["vendor_id"]: r for r in job["result"]["rows"]}
    row = rows[str(vid)]
    assert row["status"] == "proposed" and row["value"] == "2 contact(s), 1 address(es)"
    assert "1 personal mailbox(es) left out" in row["reason"] and "not found on the pages" in row["reason"]
    assert rows[str(no_site)]["status"] == "rejected" and "find it first" in rows[str(no_site)]["reason"]

    proposal = (
        await client.get(f"{API}/admin/proposals/{row['proposal_id']}", headers=curator.headers)
    ).json()
    approved = await client.post(f"{API}/admin/proposals/{row['proposal_id']}/approve",
                                 json={"version": proposal["version"]}, headers=curator.headers)  # fmt: skip
    assert approved.status_code == 200, approved.text
    vendor = (await client.get(f"{API}/vendors/{vid}", headers=curator.headers)).json()
    emails = {c["email"] for c in vendor["contacts"]}
    assert emails == {"info@fjordseals.example", "parts@fjordseals.example"}  # never the CEO's own address
    info = next(c for c in vendor["contacts"] if c["email"] == "info@fjordseals.example")
    assert info["phone"] == "+47 55 12 34 56" and info["label"] == "Head office"
    assert any(loc["address"].startswith("Strandgata 12") for loc in vendor["locations"])
