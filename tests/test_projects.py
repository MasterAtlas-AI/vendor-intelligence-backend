"""Projects, shortlists and scorecards: who can see and change what, vendor stages ranked by scorecard, weighted
totals, finalising, templates, and isolation between organizations."""

import uuid
from decimal import Decimal

from httpx import AsyncClient

from app.core.container import Container
from app.modules.projects.service import weighted_total
from tests.conftest import Factory
from tests.test_imports import _existing_vendor

API = "/api/v1"


def test_weighted_total() -> None:
    criteria = [{"key": "a", "weight": 30}, {"key": "b", "weight": 10}]
    assert weighted_total(criteria, {}) is None
    assert weighted_total(criteria, {"a": {"score": 8}}) == Decimal("80.00")  # only scored criteria count
    assert weighted_total(criteria, {"a": {"score": 8}, "b": {"score": 4}}) == Decimal("70.00")
    assert weighted_total(criteria, {"a": {"score": None}, "b": {"score": 0}}) == Decimal("0.00")


async def test_projects_shortlist_and_scorecards(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    alpha = await _existing_vendor(container, f"Alpha Pumps {tag}", f"alpha-{tag}-maitest.com")
    beta = await _existing_vendor(container, f"Beta Pumps {tag}", f"beta-{tag}-maitest.com")
    tenant = await factory.tenant()
    manager = await factory.user(tenant, ["manager"])
    engineer = await factory.user(tenant, ["engineer"])
    viewer = await factory.user(tenant, ["viewer"])

    # Engineers can't create projects; managers can, and become the owner.
    body = {"name": f"FPSO topsides {tag}", "code": f"P-{tag}", "country": "Norway", "facility_type": "FPSO"}
    assert (await client.post(f"{API}/projects", json=body, headers=engineer.headers)).status_code == 403
    created = await client.post(f"{API}/projects", json=body, headers=manager.headers)
    assert created.status_code == 201, created.text
    project = created.json()
    pid = project["id"]
    assert project["country"] == "NO" and project["my_role"] == "owner" and project["can_edit"]
    dup = await client.post(f"{API}/projects", json=body, headers=manager.headers)
    assert dup.status_code == 409

    # Not a member yet: the engineer doesn't see it.
    assert (await client.get(f"{API}/projects/{pid}", headers=engineer.headers)).status_code == 404
    assert pid not in [
        p["id"] for p in (await client.get(f"{API}/projects", headers=engineer.headers)).json()
    ]
    await client.put(f"{API}/projects/{pid}/members/{engineer.user_id}", json={"role": "editor"},
                     headers=manager.headers)  # fmt: skip
    await client.put(f"{API}/projects/{pid}/members/{viewer.user_id}", json={"role": "viewer"},
                     headers=manager.headers)  # fmt: skip
    members = (await client.get(f"{API}/projects/{pid}/members", headers=engineer.headers)).json()
    assert {m["role"] for m in members} == {"owner", "editor", "viewer"}
    # The last owner can't be demoted or removed.
    last = await client.put(f"{API}/projects/{pid}/members/{manager.user_id}", json={"role": "viewer"},
                            headers=manager.headers)  # fmt: skip
    assert last.status_code == 409

    # Editors manage vendors; viewers only read.
    add = {"vendor_id": str(alpha), "stage": "shortlist", "note": "BB2 reference on Johan Castberg"}
    assert (
        await client.post(f"{API}/projects/{pid}/vendors", json=add, headers=viewer.headers)
    ).status_code == 403
    await client.post(f"{API}/projects/{pid}/vendors", json=add, headers=engineer.headers)
    vendors = (
        await client.post(
            f"{API}/projects/{pid}/vendors", json={"vendor_id": str(beta)}, headers=engineer.headers
        )
    ).json()
    assert [(v["vendor"]["id"], v["stage"]) for v in vendors] == [
        (str(alpha), "shortlist"),
        (str(beta), "longlist"),
    ]

    # Scorecards from the default template; the weighted total and ranking follow the scores.
    templates = (await client.get(f"{API}/scorecard-templates", headers=engineer.headers)).json()
    assert templates[0]["name"] == "Technical & commercial" and len(templates[0]["criteria"]) == 6
    card_a = (await client.post(f"{API}/scorecards", json={"vendor_id": str(alpha), "project_id": pid},
                                headers=engineer.headers)).json()  # fmt: skip
    card_b = (await client.post(f"{API}/scorecards", json={"vendor_id": str(beta), "project_id": pid},
                                headers=engineer.headers)).json()  # fmt: skip
    scored = await client.patch(f"{API}/scorecards/{card_a['id']}", headers=engineer.headers, json={
        "version": card_a["version"], "scores": {"technical": {"score": 6, "comment": "OK"}, "commercial": {"score": 6}},
    })  # fmt: skip
    assert scored.status_code == 200, scored.text
    assert scored.json()["total_score"] == "60.00"
    b = (await client.patch(f"{API}/scorecards/{card_b['id']}", headers=engineer.headers, json={
        "version": card_b["version"], "scores": {"technical": {"score": 9}},
    })).json()  # fmt: skip
    assert b["total_score"] == "90.00"
    bad = await client.patch(f"{API}/scorecards/{card_b['id']}", headers=engineer.headers,
                             json={"version": b["version"], "scores": {"colour": {"score": 5}}})  # fmt: skip
    assert bad.status_code == 422
    stale = await client.patch(f"{API}/scorecards/{card_b['id']}", headers=engineer.headers,
                               json={"version": card_b["version"], "notes": "x"})  # fmt: skip
    assert stale.status_code == 409
    vendors = (await client.get(f"{API}/projects/{pid}/vendors", headers=viewer.headers)).json()
    ranks = {v["vendor"]["id"]: v["rank"] for v in vendors}
    assert ranks == {str(beta): 1, str(alpha): 2}

    # Final scorecards are locked until reopened; viewers can't edit at all.
    final = (await client.patch(f"{API}/scorecards/{card_b['id']}", headers=engineer.headers,
                                json={"version": b["version"], "status": "final"})).json()  # fmt: skip
    assert final["status"] == "final"
    locked = await client.patch(f"{API}/scorecards/{card_b['id']}", headers=engineer.headers,
                                json={"version": final["version"], "notes": "change"})  # fmt: skip
    assert locked.status_code == 409
    assert (
        await client.delete(f"{API}/scorecards/{card_b['id']}", headers=engineer.headers)
    ).status_code == 409
    assert (await client.patch(f"{API}/scorecards/{card_a['id']}", headers=viewer.headers,
                               json={"version": 2, "notes": "x"})).status_code == 403  # fmt: skip

    on_vendor = (await client.get(f"{API}/vendors/{beta}/projects", headers=engineer.headers)).json()
    assert [p["id"] for p in on_vendor] == [pid]
    cards = (
        await client.get(f"{API}/scorecards", params={"vendor_id": str(beta)}, headers=viewer.headers)
    ).json()
    assert cards[0]["project_name"] == body["name"] and cards[0]["vendor_name"] == f"Beta Pumps {tag}"

    # Another organization sees none of it (RLS), even with the ids.
    outsider = await factory.user(await factory.tenant(), ["tenant_admin"])
    assert (await client.get(f"{API}/projects/{pid}", headers=outsider.headers)).status_code == 404
    assert (await client.get(f"{API}/scorecards/{card_a['id']}", headers=outsider.headers)).status_code == 404
    assert (await client.get(f"{API}/scorecards", params={"vendor_id": str(beta)},
                             headers=outsider.headers)).json() == []  # fmt: skip

    # Archive: gone from the default list, still readable.
    proj = (await client.get(f"{API}/projects/{pid}", headers=manager.headers)).json()
    archived = await client.patch(f"{API}/projects/{pid}", json={"version": proj["version"], "status": "archived"},
                                  headers=manager.headers)  # fmt: skip
    assert archived.status_code == 200
    assert pid not in [p["id"] for p in (await client.get(f"{API}/projects", headers=manager.headers)).json()]


async def test_projects_need_an_organization(client: AsyncClient, factory: Factory) -> None:
    curator = await factory.user(None, platform_roles=["curator"])
    resp = await client.get(f"{API}/projects", headers=curator.headers)
    assert resp.status_code == 403
