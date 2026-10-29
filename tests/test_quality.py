"""Data quality: per-vendor scores and issues, the curator dashboard, freshness after re-research, and the capped
re-check of stale vendors."""

import time
import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import update

from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.db.session import open_session
from app.domain.enums import ProposalItemAction
from app.domain.principal import TenantContext
from app.modules.proposals.service import NewItem
from app.modules.vendors.models import Vendor
from tests.conftest import Factory
from tests.test_search import _eq, _vendor

API = "/api/v1"


async def test_scores_issues_and_dashboard(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    evidence = [
        {"kind": "web", "url": f"https://good-{tag}-maitest.com/p", "title": "P", "snippet": "BB2 pump"}
    ]
    good = await _vendor(container, f"Complete Pumps {tag}", "NO", [
        _eq(f"pumps.centrifugal.api610.bb{i}", f"Pump {i}", evidence=evidence) for i in range(1, 6)
    ] + [NewItem(ProposalItemAction.ADD_CERTIFICATION, {"certification_code": "ISO_9001"}, evidence=evidence)],
        website=f"good-{tag}-maitest.com", description="Maker of API 610 between-bearings pumps for offshore use.")  # fmt: skip
    bare = await _vendor(container, f"Bare Vendor {tag}", "IN", [])
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        old = datetime.now(UTC) - timedelta(days=500)
        await s.execute(update(Vendor).where(Vendor.id == bare).values(hq_country=None, created_at=old))
        await s.commit()

    curator = await factory.user(None, platform_roles=["curator"])
    assert (await client.post(f"{API}/admin/data-quality/rescore", headers=curator.headers)).json()[
        "updated"
    ] >= 2
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        good_v, bare_v = await s.get(Vendor, good), await s.get(Vendor, bare)
        assert good_v is not None and bare_v is not None
        assert good_v.data_quality_score is not None and bare_v.data_quality_score is not None
        assert float(good_v.data_quality_score) == 1.0  # everything present, sourced and fresh
        assert float(bare_v.data_quality_score) == 0.0  # nothing at all, and stale

    board = (await client.get(f"{API}/admin/data-quality", params={"issue": "stale", "limit": 200},
                              headers=curator.headers)).json()  # fmt: skip
    counts = {i["key"]: i["count"] for i in board["issues"]}
    assert counts["stale"] >= 1 and counts["no_facts"] >= 1 and board["vendors"] >= 2
    row = next(r for r in board["worst"] if r["vendor_id"] == str(bare))
    assert set(row["issues"]) >= {"no_website", "no_country", "no_description", "no_facts", "stale"}
    assert all("stale" in r["issues"] for r in board["worst"])  # filtered by issue

    engineer = await factory.user(await factory.tenant(), ["engineer"])
    assert (await client.get(f"{API}/admin/data-quality", headers=engineer.headers)).status_code == 403


async def test_reverify_queues_capped_refreshes(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    vid = await _vendor(container, f"Stale Site Co {tag}", "NO", [], website=f"stale-{tag}-maitest.com")
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        await s.execute(
            update(Vendor)
            .where(Vendor.id == vid)
            .values(created_at=datetime(2000, 1, 1, tzinfo=UTC) - timedelta(seconds=time.time()))
        )  # the stalest of all
        await s.commit()
    previous, container.ai.provider = container.ai.provider, FakeProvider()
    try:
        curator = await factory.user(None, platform_roles=["curator"])
        resp = await client.post(
            f"{API}/admin/data-quality/reverify", json={"limit": 1}, headers=curator.headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["queued"] == 1
        job = (await client.get(f"{API}/jobs/{body['job_ids'][0]}", headers=curator.headers)).json()
        assert job["type"] == "enrichment" and job["input"]["vendor_id"] == str(vid)
        again = (await client.post(f"{API}/admin/data-quality/reverify", json={"limit": 1},
                                   headers=curator.headers)).json()  # fmt: skip
        assert str(vid) not in [j for j in again["job_ids"]]  # already queued: not queued twice
    finally:
        container.ai.provider = previous
