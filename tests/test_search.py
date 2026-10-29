"""Hybrid search end to end: local query understanding, structured + full-text + vector retrieval, confidence scores
with the facts (and sources) behind each match, hard filters, the tenant's own AVL status, history and suggestions."""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient

from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.db.session import open_session
from app.domain.enums import ProposalItemAction, ProposalKind, ProposalOrigin
from app.domain.principal import TenantContext
from app.modules.proposals.service import NewItem, ProposalService
from app.modules.search.intent import parse_local
from app.modules.search.service import taxonomy
from tests.conftest import Factory
from tests.test_imports import _drain

API = "/api/v1"


@pytest.fixture
def fake_ai(container: Container) -> Iterator[FakeProvider]:
    previous, container.ai.provider = container.ai.provider, FakeProvider()
    yield container.ai.provider
    container.ai.provider = previous


async def _vendor(
    container: Container, name: str, country: str, items: list[NewItem], **extra: Any
) -> uuid.UUID:
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        p = await ProposalService(s, None).submit(
            ProposalKind.NEW_VENDOR, ProposalOrigin.MANUAL,
            [NewItem(ProposalItemAction.CREATE_VENDOR, {"legal_name": name, "hq_country": country, **extra}), *items],
            auto_approve=True,
        )  # fmt: skip
        await s.commit()
        assert p.subject_vendor_id is not None
        return p.subject_vendor_id


def _eq(code: str, name: str, **kw: Any) -> NewItem:
    evidence = kw.pop("evidence", [])
    return NewItem(
        ProposalItemAction.ADD_EQUIPMENT, {"category_code": code, "name": name, **kw}, evidence=evidence
    )


async def test_hybrid_search(
    client: AsyncClient, factory: Factory, container: Container, fake_ai: FakeProvider
) -> None:
    tag = uuid.uuid4().hex[:6]
    bb2 = await _vendor(container, f"Nordic Barrel Pumps {tag}", "NO", [
        _eq("pumps.centrifugal.api610.bb2", "HX-2", model_series="HX-2", standards=["API 610"],
            evidence=[{"kind": "web", "url": f"https://nbp-{tag}-maitest.com/pumps", "title": "Pumps",
                       "snippet": "HX-2 is an API 610 BB2 pump"}]),
        NewItem(ProposalItemAction.ADD_INDUSTRY, {"industry_code": "fpso"}),
    ], website=f"nbp-{tag}-maitest.com", description="Barrel pumps for water injection.")  # fmt: skip
    general = await _vendor(container, f"Generic Process Pumps {tag}", "DE", [
        _eq("pumps.centrifugal.api610", "API process pumps"),
    ])  # fmt: skip
    valves = await _vendor(
        container, f"Stavanger Valve Works {tag}", "NO", [_eq("valves.ball", "Ball valves")]
    )

    curator = await factory.user(None, platform_roles=["curator"])
    await _drain(container.job_runner)
    resp = await client.post(f"{API}/admin/search/reindex", json={"vendor_ids": [str(bb2), str(general), str(valves)]},
                             headers=curator.headers)  # fmt: skip
    assert resp.status_code == 202
    await container.job_runner.run_once()

    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        tax = await taxonomy(s)
    intent = parse_local("API 610 BB2 pumps for FPSO", tax)
    assert intent.equipment_categories == ["pumps.centrifugal.api610.bb2"] and intent.standards == ["API 610"]
    assert intent.industries == ["fpso"]

    tenant = await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])
    found = await client.post(f"{API}/search", json={"query": f"{tag} API 610 BB2 pumps for FPSO"},
                              headers=engineer.headers)  # fmt: skip
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["semantic"] is True and body["parser"] == "local" and body["search_id"]
    ranked = [h["vendor"]["id"] for h in body["results"]]
    assert ranked.index(str(bb2)) < ranked.index(str(general))  # exact sub-type beats the broader category
    top = body["results"][ranked.index(str(bb2))]
    assert top["scores"]["equipment"] == 1.0 and top["scores"]["industries"] == 1.0
    assert top["scores"]["standards_certifications"] == 1.0
    eq_reason = next(r for r in top["reasons"] if r["kind"] == "equipment")
    assert (
        "HX-2" in eq_reason["label"] and eq_reason["sources"][0]["snippet"] == "HX-2 is an API 610 BB2 pump"
    )
    broader = body["results"][ranked.index(str(general))]
    assert broader["scores"]["equipment"] == 0.5 and top["confidence"] > broader["confidence"]

    # Hard filter: Norway only drops the German vendor. Edited chips skip parsing.
    chips = {"equipment_categories": ["pumps"], "keywords": tag}
    norway = await client.post(f"{API}/search", json={"query": "", "intent": chips, "filters": {"countries": ["NO"]}},
                               headers=engineer.headers)  # fmt: skip
    ids = [h["vendor"]["id"] for h in norway.json()["results"]]
    assert str(bb2) in ids and str(general) not in ids

    # The organization's own list: my vendors only, with its AVL status on each hit.
    await client.put(f"{API}/my-vendors/{valves}", json={"avl_status": "approved"}, headers=engineer.headers)
    admin = await factory.user(tenant, ["tenant_admin"])
    await client.put(f"{API}/my-vendors/{valves}", json={"avl_status": "approved"}, headers=admin.headers)
    mine = await client.post(f"{API}/search", json={"query": f"valves {tag}", "filters": {"my_vendors_only": True}},
                             headers=admin.headers)  # fmt: skip
    hits = mine.json()["results"]
    assert [h["vendor"]["id"] for h in hits] == [str(valves)] and hits[0]["vendor"]["tenant"][
        "avl_status"
    ] == "approved"

    history = (await client.get(f"{API}/search/history", headers=engineer.headers)).json()
    assert "API 610 BB2" in history[0]["query"] and history[0]["result_count"] >= 2
    other = await factory.user(await factory.tenant(), ["engineer"])
    assert (await client.get(f"{API}/search/history", headers=other.headers)).json() == []  # private (RLS)
    deleted = await client.delete(f"{API}/search/history/{history[0]['id']}", headers=engineer.headers)
    assert deleted.status_code == 204

    suggestions = (
        await client.get(f"{API}/search/suggest", params={"q": "barrel"}, headers=engineer.headers)
    ).json()
    assert any(s["kind"] == "vendor" and s["value"] == str(bb2) for s in suggestions)


async def test_search_without_vectors_still_works(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    vid = await _vendor(
        container, f"Offline Compressor Co {tag}", "IN", [_eq("compressors.reciprocating", "Recip")]
    )
    previous, container.ai.provider = container.ai.provider, None
    try:
        engineer = await factory.user(await factory.tenant(), ["engineer"])
        resp = await client.post(f"{API}/search", json={"query": f"reciprocating compressors {tag}"},
                                 headers=engineer.headers)  # fmt: skip
    finally:
        container.ai.provider = previous
    body = resp.json()
    assert resp.status_code == 200 and body["semantic"] is False
    assert body["results"][0]["vendor"]["id"] == str(vid) and body["results"][0]["scores"]["semantic"] is None


async def test_ai_rerank_and_budget_fallback(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    from sqlalchemy import update

    from app.modules.search.schemas import Rerank
    from app.modules.tenants.models import Tenant

    tag = uuid.uuid4().hex[:6]
    first = await _vendor(
        container, f"Rerank Pumps One {tag}", "NO", [_eq("pumps.centrifugal.api610.bb2", "BB2 A")]
    )
    second = await _vendor(
        container, f"Rerank Pumps Two {tag}", "NO", [_eq("pumps.centrifugal.api610.bb2", "BB2 B")]
    )

    def judge(text: str) -> dict[str, Any]:
        # Prefer "Two" whatever position it came in.
        records = json.loads(text.split("Vendor records:\n<<<DATA\n", 1)[1].split("\nDATA>>>", 1)[0])
        ranking = [
            {"ref": r["ref"], "fit": 9 if "Two" in r["name"] else 3,
             "reason": "Exact BB2 maker" if "Two" in r["name"] else ""}
            for r in records
        ]  # fmt: skip
        return {"ranking": ranking}

    fake = FakeProvider().on(Rerank, judge)
    previous, container.ai.provider = container.ai.provider, fake
    try:
        tenant = await factory.tenant()
        engineer = await factory.user(tenant, ["engineer"])
        query = {"query": f"BB2 pumps {tag}", "limit": 5}
        plain = (await client.post(f"{API}/search", json=query, headers=engineer.headers)).json()
        assert plain["reranked"] is False and all(h["ai_fit"] is None for h in plain["results"])

        async with open_session(container.sessionmaker, TenantContext.platform()) as s:
            await s.execute(
                update(Tenant).where(Tenant.id == tenant).values(settings={"search_rerank": True})
            )
            await s.commit()
        ranked = (await client.post(f"{API}/search", json=query, headers=engineer.headers)).json()
        assert ranked["reranked"] is True
        ids = [h["vendor"]["id"] for h in ranked["results"]]
        assert ids.index(str(second)) < ids.index(str(first))
        top = ranked["results"][0]
        assert top["ai_fit"] == 9 and top["reasons"][0] == {"kind": "ai", "label": "Exact BB2 maker", "code": None,
                                                             "sources": []}  # fmt: skip

        # Out of AI budget: search still answers, without vectors or re-ranking.
        async with open_session(container.sessionmaker, TenantContext.platform()) as s:
            await s.execute(update(Tenant).where(Tenant.id == tenant).values(quotas={"ai_monthly_usd": 0}))
            await s.commit()
        broke = await client.post(f"{API}/search", json=query, headers=engineer.headers)
        assert (
            broke.status_code == 200
            and broke.json()["reranked"] is False
            and broke.json()["semantic"] is False
        )
        assert len(broke.json()["results"]) >= 2
    finally:
        container.ai.provider = previous
