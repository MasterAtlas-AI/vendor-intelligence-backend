"""Proposal review workflow: submit, edit, approve/reject per item, apply to the knowledge base."""

import uuid
from typing import Any

from httpx import AsyncClient

from app.core.container import Container
from app.db.session import open_session
from app.domain.enums import ProposalItemAction, ProposalKind, ProposalOrigin
from app.domain.principal import TenantContext
from app.modules.proposals.service import NewItem, ProposalService
from tests.conftest import Actor, Factory

ADMIN = "/api/v1/admin/proposals"


def new_vendor_body(name: str, domain: str, **extra_items: Any) -> dict[str, Any]:
    return {
        "kind": "new_vendor",
        "items": [
            {
                "action": "create_vendor",
                "payload": {"legal_name": f"{name} AS", "website": domain, "hq_country": "Norway"},
                "evidence": [{"kind": "web", "url": f"https://{domain}/about", "title": "About us"}],
            },
            {
                "action": "add_equipment",
                "payload": {
                    "category_code": "pumps.centrifugal.api610.bb2",
                    "name": "BB2 injection pump",
                    "model_series": "HX-2",
                    "standards": ["API 610"],
                },
                "evidence": [
                    {
                        "kind": "web",
                        "url": f"https://{domain}/pumps/hx2",
                        "title": "HX-2",
                        "snippet": "API 610 BB2",
                    }
                ],
            },
            {
                "action": "add_certification",
                "payload": {"certification_code": "API_Q1", "valid_from": "2025-01-01"},
            },
            {"action": "add_industry", "payload": {"industry_code": "fpso"}},
        ],
        **extra_items,
    }


async def curator(factory: Factory) -> Actor:
    return await factory.user(None, platform_roles=["curator"])


async def test_create_and_approve_new_vendor(client: AsyncClient, factory: Factory) -> None:
    cur = await curator(factory)
    tag = uuid.uuid4().hex[:6]
    created = await client.post(
        ADMIN, json=new_vendor_body(f"Fjord Pumps {tag}", f"fjord-{tag}-maitest.com"), headers=cur.headers
    )
    assert created.status_code == 201, created.text
    p = created.json()
    assert p["status"] == "pending_review" and p["counts"]["pending"] == 4 and p["subject_vendor"] is None
    assert p["title"] == f"Fjord Pumps {tag} AS"
    assert p["items"][0]["payload"]["website"] == f"https://fjord-{tag}-maitest.com"

    approved = await client.post(
        f"{ADMIN}/{p['id']}/approve", json={"version": p["version"]}, headers=cur.headers
    )
    assert approved.status_code == 200, approved.text
    a = approved.json()
    assert a["status"] == "approved" and a["counts"]["applied"] == 4
    vendor = a["subject_vendor"]
    assert vendor["hq_country"] == "NO" and vendor["domains"] == [f"fjord-{tag}-maitest.com"]
    assert vendor["equipment"][0]["model_series"] == "HX-2"
    assert vendor["certifications"][0]["certification"]["code"] == "API_Q1"
    assert [i["code"] for i in vendor["industries"]] == ["fpso"]
    assert {s["url"] for s in vendor["sources"]} == {
        f"https://fjord-{tag}-maitest.com/about",
        f"https://fjord-{tag}-maitest.com/pumps/hx2",
    }
    assert [h["action"] for h in a["history"]].count("approve") == 4

    # Now visible to every tenant through the normal vendor search.
    viewer = await factory.user(await factory.tenant(), ["viewer"])
    found = await client.get(
        "/api/v1/vendors", params={"equipment_category": "pumps", "q": f"fjord {tag}"}, headers=viewer.headers
    )
    assert [v["id"] for v in found.json()["items"]] == [vendor["id"]]


async def test_partial_approval_edit_and_rejection(client: AsyncClient, factory: Factory) -> None:
    cur = await curator(factory)
    tag = uuid.uuid4().hex[:6]
    p = (
        await client.post(
            ADMIN, json=new_vendor_body(f"Partial {tag}", f"partial-{tag}-maitest.com"), headers=cur.headers
        )
    ).json()
    create_id, eq_id, cert_id, ind_id = (i["id"] for i in p["items"])

    # Facts can't be approved before the vendor itself.
    early = await client.post(
        f"{ADMIN}/{p['id']}/approve", json={"item_ids": [eq_id], "version": p["version"]}, headers=cur.headers
    )
    assert early.status_code == 409 and early.json()["code"] == "vendor_first"

    # Fix a payload before approving it; invalid edits are refused.
    bad = await client.patch(
        f"{ADMIN}/{p['id']}/items/{eq_id}",
        json={"payload": {"name": "x"}, "version": p["version"]},
        headers=cur.headers,
    )
    assert bad.status_code == 422
    edited = await client.patch(
        f"{ADMIN}/{p['id']}/items/{eq_id}",
        json={
            "payload": {"category_code": "pumps.centrifugal.api610.bb3", "name": "BB3 multistage"},
            "version": p["version"],
        },
        headers=cur.headers,
    )
    assert edited.status_code == 200
    version = edited.json()["version"]

    # A stale version is refused (someone else changed it).
    stale = await client.post(
        f"{ADMIN}/{p['id']}/approve", json={"version": p["version"]}, headers=cur.headers
    )
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"

    step = await client.post(
        f"{ADMIN}/{p['id']}/approve",
        json={"item_ids": [create_id, eq_id], "version": version},
        headers=cur.headers,
    )
    assert step.json()["status"] == "pending_review" and step.json()["counts"] == {
        "pending": 2,
        "applied": 2,
        "rejected": 0,
    }
    no_reason = await client.post(
        f"{ADMIN}/{p['id']}/reject", json={"version": step.json()["version"]}, headers=cur.headers
    )
    assert no_reason.status_code == 422
    done = await client.post(
        f"{ADMIN}/{p['id']}/reject",
        json={
            "item_ids": [cert_id, ind_id],
            "note": "No evidence for these",
            "version": step.json()["version"],
        },
        headers=cur.headers,
    )
    d = done.json()
    assert d["status"] == "partially_approved"
    assert d["subject_vendor"]["equipment"][0]["category"]["code"] == "pumps.centrifugal.api610.bb3"
    assert d["subject_vendor"]["certifications"] == [] and d["subject_vendor"]["industries"] == []
    assert next(i for i in d["items"] if i["id"] == cert_id)["reject_reason"] == "No evidence for these"

    closed = await client.post(
        f"{ADMIN}/{p['id']}/approve", json={"version": d["version"]}, headers=cur.headers
    )
    assert closed.status_code == 409 and closed.json()["code"] == "proposal_closed"


async def test_rejecting_the_vendor_rejects_everything(client: AsyncClient, factory: Factory) -> None:
    cur = await curator(factory)
    tag = uuid.uuid4().hex[:6]
    p = (
        await client.post(
            ADMIN, json=new_vendor_body(f"Reject {tag}", f"reject-{tag}-maitest.com"), headers=cur.headers
        )
    ).json()
    r = await client.post(
        f"{ADMIN}/{p['id']}/reject",
        json={
            "item_ids": [p["items"][0]["id"]],
            "note": "Distributor, not a manufacturer",
            "version": p["version"],
        },
        headers=cur.headers,
    )
    assert r.json()["status"] == "rejected" and r.json()["counts"]["rejected"] == 4
    assert r.json()["subject_vendor"] is None


async def test_duplicate_website_is_refused_and_nothing_applied(
    client: AsyncClient, factory: Factory
) -> None:
    cur = await curator(factory)
    tag = uuid.uuid4().hex[:6]
    domain = f"dup-{tag}-maitest.com"
    first = (
        await client.post(ADMIN, json=new_vendor_body(f"Dup One {tag}", domain), headers=cur.headers)
    ).json()
    await client.post(
        f"{ADMIN}/{first['id']}/approve", json={"version": first["version"]}, headers=cur.headers
    )

    second = (
        await client.post(ADMIN, json=new_vendor_body(f"Dup Two {tag}", f"www.{domain}"), headers=cur.headers)
    ).json()
    clash = await client.post(
        f"{ADMIN}/{second['id']}/approve", json={"version": second["version"]}, headers=cur.headers
    )
    assert clash.status_code == 409 and clash.json()["code"] == "duplicate_vendor"
    after = (await client.get(f"{ADMIN}/{second['id']}", headers=cur.headers)).json()
    assert (
        after["status"] == "pending_review" and after["counts"]["applied"] == 0
    )  # whole decision rolled back


async def test_update_proposal_is_idempotent_per_fact(client: AsyncClient, factory: Factory) -> None:
    cur = await curator(factory)
    tag = uuid.uuid4().hex[:6]
    base = (
        await client.post(
            ADMIN, json=new_vendor_body(f"Upd {tag}", f"upd-{tag}-maitest.com"), headers=cur.headers
        )
    ).json()
    vendor_id = (
        await client.post(
            f"{ADMIN}/{base['id']}/approve", json={"version": base["version"]}, headers=cur.headers
        )
    ).json()["subject_vendor"]["id"]

    body = {
        "kind": "update_vendor",
        "subject_vendor_id": vendor_id,
        "items": [
            # Same model series as the original HX-2 entry: links to it instead of duplicating.
            {
                "action": "add_equipment",
                "payload": {
                    "category_code": "pumps.centrifugal.api610.bb2",
                    "name": "HX-2 pump",
                    "model_series": "hx-2",
                },
            },
            {
                "action": "add_service",
                "payload": {"category_code": "repair.workshop", "name": "Pump workshop repair"},
            },
            {
                "action": "update_vendor_field",
                "payload": {"field": "description", "value": "Norwegian pump maker."},
            },
            {"action": "add_alias", "payload": {"alias": f"Fjordpump {tag}"}},
        ],
    }
    p = (await client.post(ADMIN, json=body, headers=cur.headers)).json()
    assert p["title"] == f"Upd {tag} AS"
    v = (
        await client.post(f"{ADMIN}/{p['id']}/approve", json={"version": p["version"]}, headers=cur.headers)
    ).json()["subject_vendor"]
    assert v["equipment_count"] == 1 and v["service_count"] == 1
    assert v["description"] == "Norwegian pump maker." and v["aliases"] == [f"Fjordpump {tag}"]
    assert v["knowledge_version"] == 1 + 3 + 4  # created at 1, +3 facts in the first proposal, +4 here

    viewer = await factory.user(await factory.tenant(), ["viewer"])
    by_alias = await client.get("/api/v1/vendors", params={"q": f"fjordpump {tag}"}, headers=viewer.headers)
    assert [x["id"] for x in by_alias.json()["items"]] == [vendor_id]


async def test_unknown_taxonomy_code_is_rejected_at_submit(client: AsyncClient, factory: Factory) -> None:
    cur = await curator(factory)
    body = new_vendor_body("Bad Code", f"bad-{uuid.uuid4().hex[:6]}-maitest.com")
    body["items"][1]["payload"]["category_code"] = "pumps.nope"
    created = await client.post(ADMIN, json=body, headers=cur.headers)
    # Codes are checked when applied (taxonomy may change), so submit succeeds and approval fails clearly.
    assert created.status_code == 201
    p = created.json()
    resp = await client.post(
        f"{ADMIN}/{p['id']}/approve", json={"version": p["version"]}, headers=cur.headers
    )
    assert resp.status_code == 422 and "pumps.nope" in resp.text


async def test_permissions_and_tenant_view(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tenant_a, tenant_b = await factory.tenant(), await factory.tenant()
    manager_a = await factory.user(tenant_a, ["manager"])
    manager_b = await factory.user(tenant_b, ["manager"])
    assert (await client.get(ADMIN, headers=manager_a.headers)).status_code == 403
    assert (
        await client.post(ADMIN, json=new_vendor_body("X", "x-maitest.com"), headers=manager_a.headers)
    ).status_code == 403

    # An automated source (e.g. tenant A's Excel import) submits on A's behalf.
    async with open_session(container.sessionmaker, TenantContext(tenant_a)) as s:
        proposal = await ProposalService(s, None).submit(
            ProposalKind.NEW_VENDOR,
            ProposalOrigin.EXCEL,
            [NewItem(ProposalItemAction.CREATE_VENDOR, {"legal_name": f"From Excel {uuid.uuid4().hex[:6]}"})],
            origin_tenant_id=tenant_a,
        )
        await s.commit()

    mine_a = (await client.get("/api/v1/proposals/mine", headers=manager_a.headers)).json()["items"]
    assert [p["id"] for p in mine_a] == [str(proposal.id)]
    assert "origin_tenant_id" not in mine_a[0]  # tenants get the summary only
    assert (await client.get("/api/v1/proposals/mine", headers=manager_b.headers)).json()["items"] == []

    # Curators see it, including where it came from; their decisions are audited platform-wide.
    cur = await curator(factory)
    detail = (await client.get(f"{ADMIN}/{proposal.id}", headers=cur.headers)).json()
    assert detail["origin"] == "excel" and detail["origin_tenant_id"] == str(tenant_a)
    info = await client.post(
        f"{ADMIN}/{proposal.id}/request-info",
        json={"note": "Which country?", "version": detail["version"]},
        headers=cur.headers,
    )
    assert info.json()["status"] == "needs_info"
    admin = await factory.user(None, platform_roles=["super_admin"])
    logs = (
        await client.get("/api/v1/admin/audit-logs", params={"action": "proposal."}, headers=admin.headers)
    ).json()
    assert any(
        e["entity_id"] == str(proposal.id) and e["action"] == "proposal.info_requested" for e in logs["items"]
    )


async def test_auto_approve_applies_immediately(container: Container) -> None:
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        proposal = await ProposalService(s, None).submit(
            ProposalKind.NEW_VENDOR,
            ProposalOrigin.EXCEL,
            [
                NewItem(
                    ProposalItemAction.CREATE_VENDOR, {"legal_name": f"Trusted {uuid.uuid4().hex[:6]} Ltd"}
                ),
                NewItem(ProposalItemAction.ADD_INDUSTRY, {"industry_code": "lng"}),
            ],
            auto_approve=True,
        )
        await s.commit()
        assert proposal.status == "auto_approved" and proposal.subject_vendor_id is not None
