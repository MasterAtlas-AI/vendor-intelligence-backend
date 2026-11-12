"""Supplier risk watchlist (SOW 29.9): signals from certificates, the organization's own orders and evaluations,
the vendor's standing, and dependence; each flag explained."""

import uuid
from datetime import date, timedelta

from httpx import AsyncClient

from app.core.container import Container
from app.domain.enums import ProposalItemAction
from app.modules.proposals.service import NewItem
from tests.conftest import Factory
from tests.test_search import _vendor

API = "/api/v1"


async def test_supplier_risk_watchlist(client: AsyncClient, factory: Factory, container: Container) -> None:
    tag = uuid.uuid4().hex[:6]
    past = (date.today() - timedelta(days=30)).isoformat()
    soon = (date.today() + timedelta(days=45)).isoformat()
    lapsed = await _vendor(container, f"Lapsed Pumps {tag}", "NO", [
        NewItem(ProposalItemAction.ADD_CERTIFICATION, {"certification_code": "ISO_9001", "valid_to": past}),
        NewItem(ProposalItemAction.ADD_CERTIFICATION, {"certification_code": "API_Q1", "valid_to": soon}),
    ])  # fmt: skip
    late = await _vendor(container, f"Late Valves {tag}", "DE", [])
    fine = await _vendor(container, f"Fine Fans {tag}", "SG", [])
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    viewer = await factory.user(tenant, ["viewer"])
    outsider = await factory.user(await factory.tenant(), ["tenant_admin"])
    for v in (lapsed, late, fine):
        r = await client.put(f"{API}/my-vendors/{v}", headers=admin.headers, json={"avl_status": "approved"})
        assert r.status_code == 200, r.text

    project = await client.post(f"{API}/projects", headers=admin.headers,
                                json={"name": f"Risk {tag}", "kind": "historical"})  # fmt: skip
    pid = project.json()["id"]
    for n, (promised, delivered, rating, tbe) in enumerate((
        ("2024-01-10", "2024-03-01", 2, None),  # late
        ("2024-02-10", "2024-04-01", 2, None),  # late
        ("2024-03-10", "2024-03-01", 2, "rejected"),  # on time, but a failed evaluation
    )):  # fmt: skip
        r = await client.post(f"{API}/procurement-records", headers=admin.headers, json={
            "project_id": pid, "vendor_id": str(late), "po_number": f"PO-{tag}-{n}", "promised_date": promised,
            "delivered_date": delivered, "performance_rating": rating, "technical_status": tbe,
            "status": "delivered",
        })  # fmt: skip
        assert r.status_code == 201, r.text

    page = (await client.get(f"{API}/supplier-risk", headers=viewer.headers, params={"q": tag})).json()
    by_name = {r["name"]: r for r in page["items"]}
    assert set(by_name) == {f"Lapsed Pumps {tag}", f"Late Valves {tag}"}  # "Fine Fans" only has a weak flag
    lapsed_row = by_name[f"Lapsed Pumps {tag}"]
    assert lapsed_row["level"] == "high"
    assert {f["key"] for f in lapsed_row["factors"]} >= {"expired_certs", "expiring_certs"}
    late_row = by_name[f"Late Valves {tag}"]
    assert late_row["level"] == "high"
    assert {f["key"] for f in late_row["factors"]} >= {"late", "rating", "tbe_rejected"}
    assert (
        next(f for f in late_row["factors"] if f["key"] == "late")["detail"] == "2 of 3 orders delivered late"
    )

    everything = (await client.get(f"{API}/supplier-risk", headers=viewer.headers,
                                   params={"q": tag, "min_level": "low"})).json()  # fmt: skip
    fine_row = next(r for r in everything["items"] if r["name"] == f"Fine Fans {tag}")
    assert fine_row["level"] == "low" and [f["key"] for f in fine_row["factors"]] == ["stale"]
    high_only = (await client.get(f"{API}/supplier-risk", headers=viewer.headers,
                                  params={"q": tag, "min_level": "high"})).json()  # fmt: skip
    assert high_only["total"] == 2

    # Another organization's list is its own: none of these vendors are on it.
    theirs = (await client.get(f"{API}/supplier-risk", headers=outsider.headers, params={"q": tag})).json()
    assert theirs["items"] == []
