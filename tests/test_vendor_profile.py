"""Vendor profile additions (SOW review, partly-done points): manufacturing capability (5.25), support network and
factory-location filters (5.23, 16.4), source documents linked back to the vendor (17.3), the source register (24.4)."""

import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.domain.enums import ProposalItemAction
from app.modules.proposals.service import NewItem
from tests.conftest import Factory
from tests.test_imports import _drain
from tests.test_pdf_import import _run_pdf, _upload_pdf, brochure
from tests.test_search import _eq, _vendor

API = "/api/v1"
BB2 = "pumps.centrifugal.api610.bb2"


@pytest.fixture
def fake_ai(container: Container) -> Iterator[FakeProvider]:
    previous, container.ai.provider = container.ai.provider, FakeProvider()
    yield container.ai.provider
    container.ai.provider = previous


def _loc(kind: str, country: str) -> NewItem:
    return NewItem(ProposalItemAction.ADD_LOCATION, {"location_type": kind, "country": country})


async def test_capability_support_network_factories_and_sources(
    client: AsyncClient, factory: Factory, container: Container, fake_ai: FakeProvider
) -> None:
    tag = uuid.uuid4().hex[:6]
    site = f"busan-{tag}-maitest.com"
    made_in_korea = await _vendor(container, f"Busan Pumps {tag}", "NO", [
        _eq(BB2, f"BB2 pump {tag}", evidence=[{"kind": "web", "url": f"https://www.{site}/bb2", "title": "BB2"}]),
        _loc("factory", "KR"), _loc("service_center", "MY"),
    ])  # fmt: skip
    serves_malaysia = await _vendor(container, f"Penang Pumps {tag}", "SG", [
        _eq(BB2, f"BB2 pump {tag}"),
        NewItem(ProposalItemAction.ADD_SERVICE, {"category_code": "field_service", "name": "Field service",
                                                 "regions": ["Malaysia", "Indonesia"]}),
    ])  # fmt: skip
    neither = await _vendor(container, f"Bergen Pumps {tag}", "NO", [_eq(BB2, f"BB2 pump {tag}")])
    ours = [str(made_in_korea), str(serves_malaysia), str(neither)]

    curator = await factory.user(None, platform_roles=["curator"])
    await _drain(container.job_runner)
    reindex = await client.post(
        f"{API}/admin/search/reindex", json={"vendor_ids": ours}, headers=curator.headers
    )
    assert reindex.status_code == 202
    await _drain(container.job_runner)

    tenant = await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])

    async def found(filters: dict[str, object]) -> set[str]:
        resp = await client.post(f"{API}/search", headers=engineer.headers,
                                 json={"query": f"BB2 pump {tag}", "filters": filters, "limit": 50})  # fmt: skip
        assert resp.status_code == 200, resp.text
        return {h["vendor"]["id"] for h in resp.json()["results"]} & set(ours)

    assert await found({}) == set(ours)
    assert await found({"factory_countries": ["KR"]}) == {str(made_in_korea)}  # not its head office (NO)
    assert await found({"support_countries": ["Malaysia"]}) == {str(made_in_korea), str(serves_malaysia)}
    assert await found({"countries": ["NO"], "support_countries": ["my"]}) == {str(made_in_korea)}
    bad = await client.post(f"{API}/search", headers=engineer.headers,
                            json={"query": "pumps", "filters": {"factory_countries": ["Atlantis"]}})  # fmt: skip
    assert bad.status_code == 422

    # Manufacturing capability: a curator's entry goes through review like any fact.
    proposal = await client.post(f"{API}/admin/proposals", headers=curator.headers, json={
        "kind": "update_vendor", "subject_vendor_id": str(made_in_korea), "title": "Capability",
        "items": [{"action": "update_vendor_field",
                   "payload": {"field": "manufacturing_capability", "value": "Casting and machining to 12 t."},
                   "evidence": [{"kind": "manual", "title": "Entered by a curator"}]}],
    })  # fmt: skip
    assert proposal.status_code == 201, proposal.text
    p = proposal.json()
    approved = await client.post(f"{API}/admin/proposals/{p['id']}/approve", headers=curator.headers,
                                 json={"version": p["version"]})  # fmt: skip
    assert approved.status_code == 200, approved.text
    detail = (await client.get(f"{API}/vendors/{made_in_korea}", headers=engineer.headers)).json()
    assert detail["manufacturing_capability"] == "Casting and machining to 12 t."
    assert {("factory", "KR"), ("service_center", "MY")} <= {
        (loc["location_type"], loc["country"]) for loc in detail["locations"]
    }

    # Source register: grouped by domain (www. dropped); a rating applies to every source of the domain.
    assert (await client.get(f"{API}/admin/sources", headers=engineer.headers)).status_code == 403
    register = (await client.get(f"{API}/admin/sources", params={"q": site}, headers=curator.headers)).json()
    group = next(g for g in register["groups"] if g["key"] == site)
    assert (
        group["kind"] == "web"
        and group["vendors"] == 1
        and group["facts"] >= 1
        and group["reliability"] is None
    )
    rated = await client.put(f"{API}/admin/sources/reliability", headers=curator.headers,
                             json={"kind": "web", "key": site, "reliability": 0.9})  # fmt: skip
    assert rated.status_code == 200 and rated.json()["updated"] >= 1
    sources = (await client.get(f"{API}/vendors/{made_in_korea}", headers=engineer.headers)).json()["sources"]
    assert [float(s["reliability"]) for s in sources if site in (s["url"] or "")] == [0.9]


async def test_source_documents_are_linked_back_for_their_organization_only(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    domain = f"fjord-{tag}-maitest.com"
    vendor = await _vendor(container, f"Fjord {tag} Flow AS", "NO", [], website=domain)
    tenant = await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])
    outsider = await factory.user(await factory.tenant(), ["tenant_admin"])

    pdf = brochure(f"Fjord {tag} Flow AS", domain)
    up = await _upload_pdf(client, engineer, pdf)
    job = await _run_pdf(
        client, container, engineer, {"upload_id": up["id"], "vendor": {"vendor_id": str(vendor)}}
    )
    assert job["result"]["facts_selected"] >= 1

    docs = (await client.get(f"{API}/vendors/{vendor}/documents", headers=engineer.headers)).json()
    assert [(d["upload_id"], d["filename"]) for d in docs] == [(up["id"], "brochure.pdf")] and docs[0][
        "facts"
    ] >= 1
    file = await client.get(f"{API}/vendors/{vendor}/documents/{up['id']}", headers=engineer.headers)
    assert file.status_code == 200 and file.content == pdf
    assert 'filename="brochure.pdf"' in file.headers["content-disposition"]

    # Everyone who reads the vendor sees the list; the file itself needs import rights or commercial access.
    viewer = await factory.user(tenant, ["viewer"])
    planner = await factory.user(tenant, ["planning"])  # commercial:read, no imports
    listed = (await client.get(f"{API}/vendors/{vendor}/documents", headers=viewer.headers)).json()
    assert [d["upload_id"] for d in listed] == [up["id"]]
    denied = await client.get(f"{API}/vendors/{vendor}/documents/{up['id']}", headers=viewer.headers)
    assert denied.status_code == 403
    allowed = await client.get(f"{API}/vendors/{vendor}/documents/{up['id']}", headers=planner.headers)
    assert allowed.status_code == 200 and allowed.content == pdf

    # Which organization uploaded what stays private.
    assert (await client.get(f"{API}/vendors/{vendor}/documents", headers=outsider.headers)).json() == []
    other = await client.get(f"{API}/vendors/{vendor}/documents/{up['id']}", headers=outsider.headers)
    assert other.status_code == 404
