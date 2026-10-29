"""RFQs: created from a project's shortlist, issued, quoted, compared with scorecards, awarded and closed, with the
workflow guards, permissions, audit and organization isolation."""

import uuid

from httpx import AsyncClient

from app.core.container import Container
from tests.conftest import Factory
from tests.test_imports import _existing_vendor

API = "/api/v1"


async def test_rfq_lifecycle(client: AsyncClient, factory: Factory, container: Container) -> None:
    tag = uuid.uuid4().hex[:6]
    alpha = await _existing_vendor(container, f"Alpha Valves {tag}", f"alpha-{tag}-maitest.com")
    beta = await _existing_vendor(container, f"Beta Valves {tag}", f"beta-{tag}-maitest.com")
    gamma = await _existing_vendor(container, f"Gamma Valves {tag}", f"gamma-{tag}-maitest.com")
    tenant = await factory.tenant()
    manager = await factory.user(tenant, ["manager"])
    engineer = await factory.user(tenant, ["engineer"])
    viewer = await factory.user(tenant, ["viewer"])

    project = (await client.post(f"{API}/projects", json={"name": f"Topsides valves {tag}"},
                                 headers=manager.headers)).json()  # fmt: skip
    pid = project["id"]
    for vid, stage in ((alpha, "shortlist"), (beta, "selected"), (gamma, "longlist")):
        await client.post(f"{API}/projects/{pid}/vendors", json={"vendor_id": str(vid), "stage": stage},
                          headers=manager.headers)  # fmt: skip
    card = (await client.post(f"{API}/scorecards", json={"vendor_id": str(beta), "project_id": pid},
                              headers=manager.headers)).json()  # fmt: skip
    await client.patch(f"{API}/scorecards/{card['id']}", headers=manager.headers,
                       json={"version": card["version"], "scores": {"technical": {"score": 9}}})  # fmt: skip

    body = {"title": "Ball valves 2\" - 24\" API 6D", "project_id": pid, "from_shortlist": True,
            "equipment_categories": ["valves.ball"], "requirements": [{"text": "API 6D monogram"}],
            "due_date": "2026-12-01"}  # fmt: skip
    assert (await client.post(f"{API}/rfqs", json=body, headers=viewer.headers)).status_code == 403
    created = await client.post(f"{API}/rfqs", json=body, headers=engineer.headers)
    assert created.status_code == 201, created.text
    rfq = created.json()
    rid = rfq["id"]
    assert rfq["rfq_number"].startswith("RFQ-") and rfq["status"] == "draft"
    assert rfq["vendor_counts"] == {"shortlisted": 2} and rfq["project_name"] == f"Topsides valves {tag}"
    bad = await client.post(f"{API}/rfqs", json={**body, "equipment_categories": ["valves.unicorn"]},
                            headers=engineer.headers)  # fmt: skip
    assert bad.status_code == 422

    # Quotes can't be recorded before issuing; issuing invites the shortlisted vendors.
    early = await client.patch(f"{API}/rfqs/{rid}/vendors/{alpha}", json={"status": "quoted"},
                               headers=engineer.headers)  # fmt: skip
    assert early.status_code == 409
    issued = (await client.post(f"{API}/rfqs/{rid}/issue", json={"version": rfq["version"]},
                                headers=engineer.headers)).json()  # fmt: skip
    assert issued["status"] == "issued" and issued["vendor_counts"] == {"invited": 2}

    # Late addition joins as invited; quotes record amount, currency and lead time.
    await client.post(f"{API}/rfqs/{rid}/vendors", json={"vendor_id": str(gamma)}, headers=engineer.headers)
    await client.patch(f"{API}/rfqs/{rid}/vendors/{alpha}", headers=engineer.headers,
                       json={"quote_amount": "125000.00", "currency": "usd", "lead_time_weeks": 20})  # fmt: skip
    await client.patch(f"{API}/rfqs/{rid}/vendors/{beta}", headers=engineer.headers,
                       json={"quote_amount": "131500.50", "currency": "USD", "lead_time_weeks": 16})  # fmt: skip
    vendors = (await client.patch(f"{API}/rfqs/{rid}/vendors/{gamma}", json={"status": "declined"},
                                  headers=engineer.headers)).json()  # fmt: skip
    by_id = {v["vendor"]["id"]: v for v in vendors}
    assert by_id[str(alpha)]["status"] == "quoted" and by_id[str(alpha)]["currency"] == "USD"
    assert by_id[str(alpha)]["quote_received_at"] is not None and by_id[str(gamma)]["status"] == "declined"
    assert by_id[str(beta)]["score"] == "90.00"  # its project scorecard
    assert [v["vendor"]["id"] for v in vendors][:2] == [str(beta), str(alpha)]  # quoted, best score first
    assert (
        await client.delete(f"{API}/rfqs/{rid}/vendors/{gamma}", headers=engineer.headers)
    ).status_code == 409

    # Award only to a vendor that quoted; the other quoted vendor loses.
    current = (await client.get(f"{API}/rfqs/{rid}", headers=viewer.headers)).json()
    no = await client.post(f"{API}/rfqs/{rid}/award", headers=engineer.headers,
                           json={"version": current["version"], "vendor_id": str(gamma)})  # fmt: skip
    assert no.status_code == 422
    awarded = await client.post(f"{API}/rfqs/{rid}/award", headers=engineer.headers,
                                json={"version": current["version"], "vendor_id": str(beta), "note": "Best score"})  # fmt: skip
    assert awarded.status_code == 200, awarded.text
    a = awarded.json()
    assert a["status"] == "awarded" and a["awarded_vendor_name"] == f"Beta Valves {tag}"
    assert a["vendor_counts"] == {"awarded": 1, "lost": 1, "declined": 1}
    locked = await client.patch(f"{API}/rfqs/{rid}", json={"version": a["version"], "title": "x"},
                                headers=engineer.headers)  # fmt: skip
    assert locked.status_code == 409
    stale = await client.post(f"{API}/rfqs/{rid}/close", json={"version": 1}, headers=engineer.headers)
    assert stale.status_code == 409
    closed = (await client.post(f"{API}/rfqs/{rid}/close", json={"version": a["version"]},
                                headers=engineer.headers)).json()  # fmt: skip
    assert closed["status"] == "closed"
    assert (await client.post(f"{API}/rfqs/{rid}/cancel", json={"version": closed["version"]},
                              headers=engineer.headers)).status_code == 409  # fmt: skip

    listed = (await client.get(f"{API}/rfqs", params={"project_id": pid}, headers=viewer.headers)).json()
    assert [r["id"] for r in listed] == [rid]
    audit = (await client.get(f"{API}/audit-logs", params={"entity_id": rid}, headers=manager.headers)).json()
    actions = {e["action"] for e in audit["items"]}
    assert {"rfq.created", "rfq.issued", "rfq.awarded", "rfq.closed"} <= actions

    outsider = await factory.user(await factory.tenant(), ["tenant_admin"])
    assert (await client.get(f"{API}/rfqs/{rid}", headers=outsider.headers)).status_code == 404
    assert (await client.get(f"{API}/rfqs", headers=outsider.headers)).json() == []


async def test_issue_needs_vendors(client: AsyncClient, factory: Factory) -> None:
    engineer = await factory.user(await factory.tenant(), ["engineer"])
    rfq = (await client.post(f"{API}/rfqs", json={"title": "Empty"}, headers=engineer.headers)).json()
    resp = await client.post(f"{API}/rfqs/{rfq['id']}/issue", json={"version": rfq["version"]},
                             headers=engineer.headers)  # fmt: skip
    assert resp.status_code == 422
