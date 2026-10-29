"""Search vectors: the embedding job indexes stale vendors (profile + one chunk per fact), re-embeds only what changed
after an approval, drops chunks of removed facts, and the vectors answer nearest-neighbour queries."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.db.session import open_session
from app.domain.enums import ProposalItemAction, ProposalKind, ProposalOrigin
from app.domain.principal import TenantContext
from app.modules.proposals.service import NewItem, ProposalService
from app.modules.search.indexing import vendor_chunks
from app.modules.search.models import VendorEmbedding
from tests.conftest import Factory
from tests.test_imports import _drain

API = "/api/v1"


@pytest.fixture
def fake_ai(container: Container) -> Iterator[FakeProvider]:
    previous, container.ai.provider = container.ai.provider, FakeProvider()
    yield container.ai.provider
    container.ai.provider = previous


async def _vendor(container: Container, name: str, items: list[NewItem]) -> uuid.UUID:
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        p = await ProposalService(s, None).submit(
            ProposalKind.NEW_VENDOR, ProposalOrigin.MANUAL,
            [NewItem(ProposalItemAction.CREATE_VENDOR, {"legal_name": name, "hq_country": "NO",
                                                        "description": "Pump maker for offshore."}), *items],
            auto_approve=True,
        )  # fmt: skip
        await s.commit()
        assert p.subject_vendor_id is not None
        return p.subject_vendor_id


async def _reindex(
    client: AsyncClient, container: Container, curator: Any, ids: list[uuid.UUID]
) -> dict[str, Any]:
    await _drain(container.job_runner)
    resp = await client.post(f"{API}/admin/search/reindex", json={"vendor_ids": [str(i) for i in ids]},
                             headers=curator.headers)  # fmt: skip
    assert resp.status_code == 202, resp.text
    await container.job_runner.run_once()
    job = (await client.get(f"{API}/jobs/{resp.json()['job_id']}", headers=curator.headers)).json()
    assert job["status"] == "succeeded", job
    return dict(job["result"])


async def _rows(container: Container, vendor_id: uuid.UUID) -> list[VendorEmbedding]:
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        return list(await s.scalars(select(VendorEmbedding).where(VendorEmbedding.vendor_id == vendor_id)))


async def test_embedding_job_indexes_and_refreshes(
    client: AsyncClient, factory: Factory, container: Container, fake_ai: FakeProvider
) -> None:
    tag = uuid.uuid4().hex[:6]
    pumps = await _vendor(container, f"Fjord Pumps {tag}", [
        NewItem(ProposalItemAction.ADD_EQUIPMENT, {"category_code": "pumps.centrifugal.api610.bb2",
                                                   "name": "HX-2 barrel pump", "model_series": "HX-2",
                                                   "standards": ["API 610"]}),
        NewItem(ProposalItemAction.ADD_SERVICE, {"category_code": "repair", "name": "Pump overhaul"}),
    ])  # fmt: skip
    valves = await _vendor(container, f"Bergen Valves {tag}", [
        NewItem(ProposalItemAction.ADD_EQUIPMENT, {"category_code": "valves.ball", "name": "Trunnion ball valve"}),
    ])  # fmt: skip

    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        chunks = (await vendor_chunks(s, [pumps]))[pumps]
    assert [c.kind for c in chunks] == ["profile", "equipment", "service"]
    assert (
        "HX-2" in chunks[0].content
        and "API 610" in chunks[0].content
        and "Pump overhaul" in chunks[0].content
    )

    curator = await factory.user(None, platform_roles=["curator"])
    first = await _reindex(client, container, curator, [pumps, valves])
    assert first == {"vendors": 2, "embedded": 5, "reused": 0, "removed": 0}
    rows = await _rows(container, pumps)
    assert {r.kind for r in rows} == {"profile", "equipment", "service"} and len(rows[0].embedding) == 1536

    # Nothing changed: nothing is re-embedded (and no money spent).
    calls = len(fake_ai.embedded)
    assert (await _reindex(client, container, curator, [pumps]))["reused"] == 3
    assert len(fake_ai.embedded) == calls

    # A new approved fact bumps the knowledge version: the vendor is stale, only the changed chunks are embedded.
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        await ProposalService(s, None).submit(
            ProposalKind.UPDATE_VENDOR, ProposalOrigin.MANUAL,
            [NewItem(ProposalItemAction.ADD_EQUIPMENT, {"category_code": "pumps.centrifugal.firewater",
                                                        "name": "Firewater pump package"})],
            subject_vendor_id=pumps, auto_approve=True,
        )  # fmt: skip
        await s.commit()
    status = (await client.get(f"{API}/admin/search/index", headers=curator.headers)).json()
    assert status["stale_vendors"] >= 1
    again = await _reindex(client, container, curator, [pumps])
    assert (
        again["embedded"] == 2 and again["reused"] == 2
    )  # new equipment chunk + the profile that mentions it

    # Nearest neighbour: a firewater query lands on the pump vendor, a ball-valve query on the valve vendor.
    query_vec = (await fake_ai.embed(["firewater pump package"])).vectors[0]
    valve_vec = (await fake_ai.embed(["trunnion ball valve"])).vectors[0]
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        ids = [pumps, valves]
        near = await s.scalar(
            select(VendorEmbedding.vendor_id).where(VendorEmbedding.vendor_id.in_(ids))
            .order_by(VendorEmbedding.embedding.cosine_distance(query_vec)).limit(1)
        )  # fmt: skip
        near_valve = await s.scalar(
            select(VendorEmbedding.vendor_id).where(VendorEmbedding.vendor_id.in_(ids))
            .order_by(VendorEmbedding.embedding.cosine_distance(valve_vec)).limit(1)
        )  # fmt: skip
    assert near == pumps and near_valve == valves


async def test_reindex_is_for_curators(client: AsyncClient, factory: Factory) -> None:
    tenant = await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])
    resp = await client.post(f"{API}/admin/search/reindex", json={}, headers=engineer.headers)
    assert resp.status_code == 403
