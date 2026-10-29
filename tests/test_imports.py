"""Excel import end to end: upload -> mapping preview -> background job -> report -> tenant list and proposals.

Tests drive the job runner explicitly (`run_once`), like test_jobs. Vendor names and domains carry a random tag so
tests don't collide with each other's data in the shared test database.
"""

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import update

from app.core.container import Container
from app.db.session import open_session
from app.domain.enums import ProposalItemAction, ProposalKind, ProposalOrigin
from app.domain.principal import TenantContext
from app.modules.proposals.service import NewItem, ProposalService
from app.modules.tenants.models import Tenant
from app.workers.runner import JobRunner
from tests.conftest import Actor, Factory
from tests.test_import_parsing import xlsx

API = "/api/v1"
HEADER = ["Supplier Name", "Website", "Country", "Products", "Vendor Code", "AVL Status", "Notes"]
MAPPING = {
    "name": 0,
    "website": 1,
    "country": 2,
    "equipment": 3,
    "internal_code": 4,
    "avl_status": 5,
    "notes": 6,
}


async def _drain(runner: JobRunner) -> None:
    while await runner.run_once():
        pass


async def _existing_vendor(container: Container, name: str, domain: str) -> uuid.UUID:
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        proposal = await ProposalService(s, None).submit(
            ProposalKind.NEW_VENDOR,
            ProposalOrigin.MANUAL,
            [NewItem(ProposalItemAction.CREATE_VENDOR, {"legal_name": name, "website": domain})],
            auto_approve=True,
        )
        await s.commit()
        assert proposal.subject_vendor_id is not None
        return proposal.subject_vendor_id


async def _upload(client: AsyncClient, who: Actor, rows: list[list[Any]], name: str = "vendors.xlsx") -> Any:
    return await client.post(
        f"{API}/imports/uploads",
        files={
            "file": (name, xlsx(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        },
        headers=who.headers,
    )


async def _import(client: AsyncClient, container: Container, who: Actor, upload_id: str) -> dict[str, Any]:
    await _drain(container.job_runner)
    started = await client.post(
        f"{API}/imports/excel", json={"upload_id": upload_id, "mapping": MAPPING}, headers=who.headers
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]
    assert await container.job_runner.run_once() == uuid.UUID(job_id)
    job = (await client.get(f"{API}/jobs/{job_id}", headers=who.headers)).json()
    assert job["status"] == "succeeded", job
    return dict(job)


async def _vendor(client: AsyncClient, who: Actor, vendor_id: Any) -> dict[str, Any]:
    resp = await client.get(f"{API}/vendors/{vendor_id}", headers=who.headers)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def test_excel_import_end_to_end(client: AsyncClient, factory: Factory, container: Container) -> None:
    tag = uuid.uuid4().hex[:6]
    tenant, other_tenant = await factory.tenant(), await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])
    outsider = await factory.user(other_tenant, ["engineer"])
    existing = await _existing_vendor(container, f"Existing {tag} Ltd", f"existing-{tag}-maitest.com")

    up = await _upload(
        client,
        engineer,
        [
            [f"Vendor list {tag}"],
            HEADER,
            [f"Existing {tag} Ltd", f"https://existing-{tag}-maitest.com", "Norway", "Centrifugal pumps",
             "V-1", "Approved", "Key supplier"],
            [f"Newco {tag} AS", f"newco-{tag}-maitest.com", "NO", "", "V-2", "conditional", ""],
            [f"NEWCO {tag}", f"http://www.newco-{tag}-maitest.com/en", "", "", "", "", ""],
            ["", "nameless-maitest.com", "", "", "", "", ""],
            [f"Nameonly {tag}", "", "Atlantis", "Flux capacitors", "", "", ""],
        ],
    )  # fmt: skip
    assert up.status_code == 201, up.text
    sheet = up.json()["sheets"][0]
    assert sheet["header_row"] == 2 and sheet["data_rows"] == 5
    assert sheet["suggested_mapping"] == MAPPING
    upload_id = up.json()["id"]

    # Other tenants can't see the file.
    assert (
        await client.get(f"{API}/imports/uploads/{upload_id}", headers=outsider.headers)
    ).status_code == 404

    preview = await client.post(
        f"{API}/imports/excel/preview",
        json={"upload_id": upload_id, "mapping": MAPPING},
        headers=engineer.headers,
    )
    assert preview.status_code == 200, preview.text
    p = preview.json()
    assert [r["outcome"] for r in p["rows"]] == [
        "matched",
        "proposed",
        "duplicate_in_file",
        "invalid",
        "proposed",
    ]
    assert p["rows"][0]["vendor_id"] == str(existing) and "2 new fact" in p["rows"][0]["messages"][0]

    job = await _import(client, container, engineer, upload_id)
    counts = job["result"]["counts"]
    assert job["result"]["rows"] == 5
    assert {k: v for k, v in counts.items() if v} == {
        "matched": 1,
        "proposed": 2,
        "duplicate_in_file": 1,
        "invalid": 1,
    }

    report = (await client.get(f"{API}/imports/{job['id']}/rows", headers=engineer.headers)).json()
    rows = report["items"]
    assert [r["row_number"] for r in rows] == [3, 4, 5, 6, 7]
    matched, newco, dup, invalid, nameonly = rows
    assert matched["vendor_id"] == str(existing) and matched["proposal_id"] is not None
    assert newco["vendor_id"] is None and newco["overlay"] == {
        "avl_status": "conditional",
        "internal_vendor_code": "V-2",
    }
    assert "row 4" in dup["messages"][0] and invalid["messages"] == ["No vendor name"]
    assert any("Atlantis" in m for m in nameonly["messages"])
    assert any("Flux capacitors" in m for m in nameonly["messages"])
    page = (
        await client.get(f"{API}/imports/{job['id']}/rows", params={"limit": 2}, headers=engineer.headers)
    ).json()
    assert page["has_more"] and page["next_cursor"] == "4"
    assert (await client.get(f"{API}/imports/{job['id']}/rows", headers=outsider.headers)).status_code == 404

    csv_report = await client.get(f"{API}/imports/{job['id']}/report.csv", headers=engineer.headers)
    assert csv_report.status_code == 200 and "duplicate_in_file" in csv_report.text

    # The existing vendor is on the tenant's list with the file's own fields; the new fact awaits review.
    vendor = await _vendor(client, engineer, existing)
    assert vendor["tenant"]["avl_status"] == "approved"
    assert vendor["tenant"]["internal_vendor_code"] == "V-1" and vendor["tenant"]["notes"] == "Key supplier"
    assert vendor["equipment"] == []
    assert (await _vendor(client, outsider, existing))["tenant"] is None

    # A curator approves the new vendor: it joins the importing tenant's list straight away.
    curator = await factory.user(None, platform_roles=["curator"])
    proposal = (
        await client.get(f"{API}/admin/proposals/{newco['proposal_id']}", headers=curator.headers)
    ).json()
    assert proposal["origin"] == "excel" and proposal["origin_tenant_id"] == str(tenant)
    assert proposal["items"][0]["payload"]["hq_country"] == "NO"
    approved = await client.post(
        f"{API}/admin/proposals/{proposal['id']}/approve",
        json={"version": proposal["version"]},
        headers=curator.headers,
    )
    assert approved.status_code == 200, approved.text
    new_vendor_id = approved.json()["subject_vendor"]["id"]
    linked = (await client.get(f"{API}/imports/{job['id']}/rows", params={"outcome": "proposed"}, headers=engineer.headers)).json()  # fmt: skip
    assert linked["items"][0]["vendor_id"] == new_vendor_id
    assert (await _vendor(client, engineer, new_vendor_id))["tenant"]["avl_status"] == "conditional"

    # Importing the same file again: the reviewed vendor now matches, the other is already awaiting review.
    again = await _import(client, container, engineer, upload_id)
    assert again["result"]["counts"]["matched"] == 2
    assert again["result"]["counts"]["already_proposed"] == 1


async def test_trusted_tenant_import_goes_straight_into_the_knowledge_base(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    tenant = await factory.tenant()
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        await s.execute(update(Tenant).where(Tenant.id == tenant).values(trusted_source=True))
        await s.commit()
    manager = await factory.user(tenant, ["manager"])
    existing = await _existing_vendor(container, f"Known {tag} GmbH", f"known-{tag}-maitest.com")

    up = await _upload(
        client,
        manager,
        [
            HEADER,
            [f"Known {tag} GmbH", "", "DE", "Gate valves; Ball valves", "K-9", "", ""],
            [f"Trusted New {tag} Ltd", f"trusted-new-{tag}-maitest.com", "GB", "Ball valves", "", "blacklisted", ""],
        ],
    )  # fmt: skip
    job = await _import(client, container, manager, up.json()["id"])
    counts = {k: v for k, v in job["result"]["counts"].items() if v}
    assert counts == {"matched": 1, "created": 1}

    known = await _vendor(client, manager, existing)
    assert {e["category"]["code"].split(".")[-1] for e in known["equipment"]} == {"gate", "ball"}
    assert known["hq_country"] == "DE"  # missing facts were filled in (auto-approved)
    assert known["tenant"]["internal_vendor_code"] == "K-9"

    rows = (await client.get(f"{API}/imports/{job['id']}/rows", headers=manager.headers)).json()["items"]
    created = await _vendor(client, manager, rows[1]["vendor_id"])
    assert created["domains"] == [f"trusted-new-{tag}-maitest.com"] and created["hq_country"] == "GB"
    assert created["tenant"]["avl_status"] == "blacklisted"


async def test_upload_validation_and_permissions(client: AsyncClient, factory: Factory) -> None:
    tenant = await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])
    viewer = await factory.user(tenant, ["viewer"])

    assert (await _upload(client, viewer, [HEADER, ["A", "", "", "", "", "", ""]])).status_code == 403
    bad = await client.post(
        f"{API}/imports/uploads", files={"file": ("list.xls", b"\xd0\xcf\x11\xe0", "application/vnd.ms-excel")},
        headers=engineer.headers,
    )  # fmt: skip
    assert bad.status_code == 422 and "xlsx" in bad.json()["detail"]
    assert (await _upload(client, engineer, [HEADER])).status_code == 422  # no data rows

    up = (await _upload(client, engineer, [HEADER, ["Acme", "", "", "", "", "", ""]])).json()
    no_name = await client.post(
        f"{API}/imports/excel",
        json={"upload_id": up["id"], "mapping": {"website": 1}},
        headers=engineer.headers,
    )
    assert no_name.status_code == 422
    beyond = await client.post(
        f"{API}/imports/excel/preview", json={"upload_id": up["id"], "mapping": {"name": 0, "notes": 12}},
        headers=engineer.headers,
    )  # fmt: skip
    assert beyond.status_code == 422 and beyond.json()["errors"][0]["field"] == "mapping.notes"
    missing = await client.post(
        f"{API}/imports/excel",
        json={"upload_id": str(uuid.uuid4()), "mapping": {"name": 0}},
        headers=engineer.headers,
    )
    assert missing.status_code == 404
