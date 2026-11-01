"""Analytics: the organization view counts only that organization's data; the platform view covers the knowledge
base, the discovery funnel, approval turnaround and every organization; each is limited to its audience."""

import uuid

from httpx import AsyncClient

from app.core.container import Container
from tests.conftest import Factory
from tests.test_search import _eq, _vendor

API = "/api/v1"


async def test_analytics(client: AsyncClient, factory: Factory, container: Container) -> None:
    tag = uuid.uuid4().hex[:6]
    pump = await _vendor(
        container, f"Analytic Pumps {tag}", "NO", [_eq("pumps.centrifugal.api610.bb2", "BB2")]
    )
    valve = await _vendor(container, f"Analytic Valves {tag}", "IN", [_eq("valves.ball", "Ball valves")])
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    viewer = await factory.user(tenant, ["viewer"])
    await client.put(f"{API}/my-vendors/{pump}", json={"avl_status": "approved"}, headers=admin.headers)
    await client.put(f"{API}/my-vendors/{valve}", json={"avl_status": "conditional"}, headers=admin.headers)
    await client.post(f"{API}/search", json={"query": f"BB2 pumps {tag}"}, headers=admin.headers)
    await client.post(f"{API}/rfqs", json={"title": "Pumps"}, headers=admin.headers)

    org = await client.get(f"{API}/analytics", params={"days": 7}, headers=viewer.headers)
    assert org.status_code == 200, org.text
    o = org.json()
    assert o["vendors"] == 2 and o["searches"] == 1 and o["days"] == 7
    assert {b["label"]: b["value"] for b in o["by_avl_status"]} == {"approved": 1, "conditional": 1}
    assert {b["label"] for b in o["by_category"]} == {"Pumps", "Valves"}
    assert {b["label"]: b["value"] for b in o["by_country"]} == {"NO": 1, "IN": 1}
    assert o["rfqs_by_status"] == [{"label": "draft", "value": 1}] and o["ai_budget_month"] == 25.0

    other = await factory.user(await factory.tenant(), ["tenant_admin"])
    empty = (await client.get(f"{API}/analytics", headers=other.headers)).json()
    assert empty["vendors"] == 0 and empty["searches"] == 0 and empty["rfqs_by_status"] == []

    assert (await client.get(f"{API}/admin/analytics", headers=admin.headers)).status_code == 403
    curator = await factory.user(None, platform_roles=["curator"])
    assert (await client.get(f"{API}/analytics", headers=curator.headers)).status_code == 403
    platform = await client.get(f"{API}/admin/analytics", params={"days": 30}, headers=curator.headers)
    assert platform.status_code == 200, platform.text
    p = platform.json()
    kb = p["knowledge_base"]
    assert kb["vendors"] >= 2 and kb["with_equipment"] >= 2 and kb["added"] >= 2
    assert {"Pumps", "Valves"} <= {b["label"] for b in kb["by_category"]}
    assert [s["label"] for s in p["discovery_funnel"]["steps"]] == [
        "Candidates found", "Researched", "Proposed or matched", "Approved by curators"]  # fmt: skip
    assert any(
        a["origin"] == "manual" for a in p["approvals"]
    )  # the test vendors came in as manual proposals
    mine = next(t for t in p["organizations"] if t["id"] == str(tenant))
    assert mine["vendors"] == 2 and mine["searches"] == 1 and mine["rfqs"] == 1 and mine["users"] == 2
