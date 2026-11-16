"""Bulk approve / reject on the approvals queue: each proposal succeeds or fails on its own."""

import uuid
from typing import Any

from httpx import AsyncClient

from app.core.container import Container
from tests.conftest import Factory
from tests.test_imports import _existing_vendor
from tests.test_search import _vendor

API = "/api/v1"


async def _website_proposal(
    client: AsyncClient, headers: dict[str, str], vid: uuid.UUID, site: str
) -> dict[str, Any]:
    resp = await client.post(f"{API}/admin/proposals", headers=headers, json={
        "kind": "update_vendor", "subject_vendor_id": str(vid),
        "items": [{"action": "update_vendor_field", "payload": {"field": "website", "value": site}}],
    })  # fmt: skip
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


async def test_bulk_decisions(client: AsyncClient, factory: Factory, container: Container) -> None:
    tag = uuid.uuid4().hex[:6]
    curator = await factory.user(None, platform_roles=["curator"])
    vendors = [await _vendor(container, f"Bulk {i} {tag}", "NO", []) for i in range(5)]
    await _existing_vendor(container, f"Owner {tag}", f"owned-{tag}-maitest.com")
    a = await _website_proposal(client, curator.headers, vendors[0], f"https://bulk-a-{tag}-maitest.com")
    b = await _website_proposal(client, curator.headers, vendors[1], f"https://bulk-b-{tag}-maitest.com")
    clash = await _website_proposal(client, curator.headers, vendors[2], f"https://owned-{tag}-maitest.com")
    stale = await _website_proposal(client, curator.headers, vendors[3], f"https://bulk-d-{tag}-maitest.com")

    engineer = await factory.user(await factory.tenant(), ["engineer"])
    body = {"action": "approve", "proposals": [{"id": a["id"], "version": a["version"]}]}
    assert (
        await client.post(f"{API}/admin/proposals/bulk", json=body, headers=engineer.headers)
    ).status_code == 403

    body = {"action": "approve", "proposals": [
        {"id": p["id"], "version": p["version"]} for p in (a, clash, b)
    ] + [{"id": stale["id"], "version": stale["version"] + 7}]}  # fmt: skip
    resp = await client.post(f"{API}/admin/proposals/bulk", json=body, headers=curator.headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["done"] == 2 and result["failed"] == 2
    by_id = {r["id"]: r for r in result["results"]}
    assert by_id[a["id"]]["ok"] and by_id[b["id"]]["ok"]  # the ones after a failure still go through
    assert f"Owner {tag}" in by_id[clash["id"]]["error"]
    assert "changed since you loaded it" in by_id[stale["id"]]["error"]
    vendor = (await client.get(f"{API}/vendors/{vendors[1]}", headers=curator.headers)).json()
    assert vendor["website"] == f"https://bulk-b-{tag}-maitest.com"

    reject = {"action": "reject", "proposals": [{"id": clash["id"], "version": clash["version"]}]}
    assert (
        await client.post(f"{API}/admin/proposals/bulk", json=reject, headers=curator.headers)
    ).status_code == 422
    resp = await client.post(f"{API}/admin/proposals/bulk", json={**reject, "note": "Parent group's site"},
                             headers=curator.headers)  # fmt: skip
    assert resp.json()["done"] == 1 and resp.json()["results"][0]["status"] == "rejected"


async def test_bulk_save_as_group_websites(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    """A website another vendor owns (group companies on the parent's site) is refused by a plain approval and
    saved as a shared group website with `shared_websites`; the owner keeps its domain."""
    curator = await factory.user(None, platform_roles=["curator"])
    tag = uuid.uuid4().hex[:6]
    owner = await _existing_vendor(container, f"Group Parent {tag}", f"group-{tag}-maitest.com")
    child = await _vendor(container, f"Group Child {tag}", "SG", [])
    p = await _website_proposal(client, curator.headers, child, f"https://group-{tag}-maitest.com")
    url = f"{API}/admin/proposals/bulk"

    refused = (await client.post(url, headers=curator.headers, json={
        "action": "approve", "proposals": [{"id": p["id"], "version": p["version"]}],
    })).json()  # fmt: skip
    assert refused["failed"] == 1 and "already belongs to" in refused["results"][0]["error"]

    shared = (await client.post(url, headers=curator.headers, json={
        "action": "approve", "shared_websites": True, "proposals": [{"id": p["id"], "version": p["version"]}],
    })).json()  # fmt: skip
    assert shared["done"] == 1, shared
    child_v = (await client.get(f"{API}/vendors/{child}", headers=curator.headers)).json()
    owner_v = (await client.get(f"{API}/vendors/{owner}", headers=curator.headers)).json()
    assert child_v["website"] == f"https://group-{tag}-maitest.com"
    assert (
        f"group-{tag}-maitest.com" in owner_v["domains"]
        and f"group-{tag}-maitest.com" not in child_v["domains"]
    )
