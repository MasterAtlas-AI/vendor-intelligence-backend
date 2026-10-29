"""Duplicate detection: fuzzy matching in imports, the scan job's merge proposals, the AI judge for the grey zone, and
merging (global facts on approval, organizations' own records right after)."""

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select, text

from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.db.session import open_session
from app.discovery.schemas import SameCompany
from app.domain.enums import ProposalItemAction, ProposalKind, ProposalOrigin
from app.domain.principal import TenantContext
from app.modules.proposals.service import NewItem, ProposalService
from app.modules.vendors.models import Vendor, VendorEquipment
from tests.conftest import Factory
from tests.test_imports import _drain, _upload, _vendor

API = "/api/v1"


async def _vendor_with(container: Container, name: str, *, domain: str | None = None,
                       equipment: list[str] | None = None) -> uuid.UUID:  # fmt: skip
    items = [NewItem(ProposalItemAction.CREATE_VENDOR, {"legal_name": name, "website": domain})]
    items += [NewItem(ProposalItemAction.ADD_EQUIPMENT, {"category_code": c, "name": c.split(".")[-1]})
              for c in equipment or []]  # fmt: skip
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        p = await ProposalService(s, None).submit(ProposalKind.NEW_VENDOR, ProposalOrigin.MANUAL, items,
                                                  auto_approve=True)  # fmt: skip
        await s.commit()
        assert p.subject_vendor_id is not None
        return p.subject_vendor_id


async def _similarity(container: Container, a: str, b: str) -> float:
    from app.domain.normalization import normalize_name

    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        return float(
            await s.scalar(
                text("select similarity(:a, :b)"), {"a": normalize_name(a), "b": normalize_name(b)}
            )
        )


async def _run_scan(client: AsyncClient, container: Container, curator: Any, **body: Any) -> dict[str, Any]:
    await _drain(container.job_runner)
    started = await client.post(
        f"{API}/admin/dedup/scan", json={"max_pairs": 1000, **body}, headers=curator.headers
    )
    assert started.status_code == 202, started.text
    await container.job_runner.run_once()
    job = (await client.get(f"{API}/jobs/{started.json()['job_id']}", headers=curator.headers)).json()
    assert job["status"] == "succeeded", job
    return dict(job["result"])


async def _merge_proposals_for(
    client: AsyncClient, curator: Any, vendor_id: uuid.UUID
) -> list[dict[str, Any]]:
    page = (await client.get(f"{API}/admin/proposals", params={"kind": "merge_vendors", "limit": 100},
                             headers=curator.headers)).json()  # fmt: skip
    out = []
    for summary in page["items"]:
        if summary["subject_vendor_id"] == str(vendor_id):
            out.append(
                (await client.get(f"{API}/admin/proposals/{summary['id']}", headers=curator.headers)).json()
            )
    return out


async def test_import_matches_near_identical_names(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    existing_name = f"Trondheim Hydraulic Pumpworks {tag}"
    vendor_id = await _vendor_with(container, existing_name)
    typo = f"Trondheim Hydraulic Pumpwork {tag}"
    assert await _similarity(container, existing_name, typo) >= 0.92

    tenant = await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])
    up = (await _upload(client, engineer, [["Vendor", "Website"], [typo, ""]])).json()
    preview = await client.post(f"{API}/imports/excel/preview", json={"upload_id": up["id"], "mapping": {"name": 0}},
                                headers=engineer.headers)  # fmt: skip
    row = preview.json()["rows"][0]
    assert row["outcome"] == "matched" and row["vendor_id"] == str(vendor_id)


async def test_scan_proposes_merge_and_approval_merges_everything(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    keep_id = await _vendor_with(container, f"Bergen Subsea Systems {tag} AS", domain=f"bss-{tag}-maitest.com",
                                 equipment=["pumps.centrifugal.firewater", "valves.ball"])  # fmt: skip
    dup_id = await _vendor_with(container, f"Bergen Subsea Systems {tag} Ltd",
                                equipment=["valves.ball", "valves.gate"])  # fmt: skip

    tenant_a, tenant_b = await factory.tenant(), await factory.tenant()
    admin_a = await factory.user(tenant_a, ["tenant_admin"])
    admin_b = await factory.user(tenant_b, ["tenant_admin"])
    await client.put(f"{API}/my-vendors/{dup_id}", json={"avl_status": "approved", "internal_vendor_code": "A-1",
                                                         "tags": ["subsea"]}, headers=admin_a.headers)  # fmt: skip
    await client.put(f"{API}/my-vendors/{keep_id}", json={"tags": ["valves"]}, headers=admin_b.headers)
    await client.put(f"{API}/my-vendors/{dup_id}", json={"avl_status": "conditional", "tags": ["gate"],
                                                         "notes": "met at ONS"}, headers=admin_b.headers)  # fmt: skip

    curator = await factory.user(None, platform_roles=["curator"])
    result = await _run_scan(client, container, curator, use_ai=False, min_similarity=0.9)
    assert result["proposed"] >= 1
    [proposal] = await _merge_proposals_for(client, curator, keep_id)  # survivor: more facts and a website
    item = proposal["items"][0]
    assert item["action"] == "merge_vendor" and item["payload"]["duplicate_vendor_id"] == str(dup_id)
    assert proposal["origin"] == "dedup_scan" and "nearly identical" in item["payload"]["reason"]

    again = await _run_scan(client, container, curator, use_ai=False, min_similarity=0.9)
    assert len(await _merge_proposals_for(client, curator, keep_id)) == 1 and again["already_known"] >= 1

    approved = await client.post(f"{API}/admin/proposals/{proposal['id']}/approve",
                                 json={"version": proposal["version"]}, headers=curator.headers)  # fmt: skip
    assert approved.status_code == 200, approved.text

    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        dup = await s.get(Vendor, dup_id)
        assert dup is not None and dup.status == "retired" and dup.merged_into_id == keep_id
        live = list(await s.scalars(select(VendorEquipment).where(VendorEquipment.vendor_id == keep_id,
                                                                    VendorEquipment.status == "approved")))  # fmt: skip
        assert len(live) == 3  # firewater + ball (kept once) + gate (moved over)

    a_view = (await _vendor(client, admin_a, keep_id))["tenant"]
    assert a_view["avl_status"] == "approved" and a_view["internal_vendor_code"] == "A-1"  # moved over
    b_view = (await _vendor(client, admin_b, keep_id))["tenant"]
    assert b_view["avl_status"] == "conditional" and b_view["tags"] == ["gate", "valves"]  # combined
    assert "met at ONS" in b_view["notes"]
    search = (
        await client.get(f"{API}/vendors", params={"q": f"Bergen Subsea {tag}"}, headers=admin_a.headers)
    ).json()
    assert [v["id"] for v in search["items"]] == [str(keep_id)]  # the duplicate no longer shows up


async def test_ai_judges_the_grey_zone(client: AsyncClient, factory: Factory, container: Container) -> None:
    tag = uuid.uuid4().hex[:6]
    first = await _vendor_with(container, f"Stavanger Valve Company {tag}")
    second = await _vendor_with(container, f"Stavanger Valves {tag} Holding")
    sim = await _similarity(container, f"Stavanger Valve Company {tag}", f"Stavanger Valves {tag} Holding")
    assert 0.5 <= sim < 0.92, sim

    verdicts = {"same": 0}

    def decide(text_in: str) -> dict[str, Any]:
        verdicts["same"] += 1
        return {
            "relationship": "parent_subsidiary",
            "confidence": 0.9,
            "reason": "Holding company and subsidiary",
        }

    fake = FakeProvider().on(SameCompany, decide)
    previous, container.ai.provider = container.ai.provider, fake
    try:
        curator = await factory.user(None, platform_roles=["curator"])
        result = await _run_scan(client, container, curator, use_ai=True, min_similarity=0.5)
    finally:
        container.ai.provider = previous
    assert verdicts["same"] >= 1 and result["ai_checked"] >= 1 and result["not_duplicates"] >= 1
    assert await _merge_proposals_for(client, curator, first) == []
    assert await _merge_proposals_for(client, curator, second) == []
