"""Project documents (SOW 3.13 drawings, 3.14 correspondence, 3.17 / 14.6 schedules) and lead-time / cost
benchmarks from procurement records (SOW 29.5)."""

import uuid

from httpx import AsyncClient

from tests.conftest import Factory

API = "/api/v1"
BB2 = "pumps.centrifugal.api610.bb2"


async def _project_with_line(client: AsyncClient, admin_headers: dict[str, str], tag: str) -> tuple[str, str]:
    project = await client.post(f"{API}/projects", headers=admin_headers, json={
        "name": f"FPSO Docs {tag}", "kind": "historical", "year": 2022, "location": "offshore",
    })  # fmt: skip
    assert project.status_code == 201, project.text
    pid = project.json()["id"]
    line = await client.post(f"{API}/equipment", headers=admin_headers, json={
        "project_id": pid, "name": "Seawater lift pump", "tag": f"P-{tag}", "category_code": BB2, "quantity": 2,
    })  # fmt: skip
    assert line.status_code == 201, line.text
    return pid, line.json()["id"]


async def test_project_documents_register(client: AsyncClient, factory: Factory) -> None:
    tag = uuid.uuid4().hex[:6]
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    viewer = await factory.user(tenant, ["viewer"])
    outsider = await factory.user(await factory.tenant(), ["tenant_admin"])
    pid, line_id = await _project_with_line(client, admin.headers, tag)

    drawing = b"%PDF-1.4 GA drawing"
    added = await client.post(
        f"{API}/projects/{pid}/documents",
        headers=admin.headers,
        files={"file": ("GA-P101.pdf", drawing, "application/pdf")},
        data={
            "doc_type": "drawing",
            "doc_number": f"20171-NETZ-{tag}-DW-0001",
            "revision": "Z1",
            "equipment_id": line_id,
            "doc_date": "2021-05-04",
        },
    )
    assert added.status_code == 201, added.text
    doc = added.json()
    assert doc["title"] == "GA-P101.pdf" and doc["equipment_label"] == f"P-{tag} · Seawater lift pump"
    letter = await client.post(f"{API}/projects/{pid}/documents", headers=admin.headers,
                               files={"file": ("letter.msg", b"From: vendor", "application/vnd.ms-outlook")},
                               data={"doc_type": "correspondence", "title": "Delay notice"})  # fmt: skip
    assert letter.status_code == 201, letter.text
    schedule = await client.post(f"{API}/projects/{pid}/documents", headers=admin.headers,
                                 files={"file": ("schedule.xlsx", b"PK..", "application/octet-stream")},
                                 data={"doc_type": "schedule", "title": "Package schedule", "equipment_id": line_id})  # fmt: skip
    assert schedule.status_code == 201

    # Filters: by type, by line, by number text. A viewer reads; only data administrators add or delete.
    async def titles(**query: str) -> list[str]:
        r = await client.get(f"{API}/projects/{pid}/documents", headers=viewer.headers, params=query)
        assert r.status_code == 200, r.text
        return sorted(d["title"] for d in r.json())

    assert await titles() == ["Delay notice", "GA-P101.pdf", "Package schedule"]
    assert await titles(doc_type="correspondence") == ["Delay notice"]
    assert await titles(equipment_id=line_id) == ["GA-P101.pdf", "Package schedule"]
    assert await titles(q=f"NETZ-{tag}") == ["GA-P101.pdf"]
    file = await client.get(f"{API}/project-documents/{doc['id']}/file", headers=viewer.headers)
    assert file.status_code == 200 and file.content == drawing
    denied = await client.post(f"{API}/projects/{pid}/documents", headers=viewer.headers,
                               files={"file": ("x.pdf", b"x", "application/pdf")}, data={"doc_type": "other"})  # fmt: skip
    assert denied.status_code == 403

    # A line from another project can't be linked; another organization sees nothing.
    other_pid, other_line = await _project_with_line(client, admin.headers, tag + "b")
    wrong = await client.patch(f"{API}/project-documents/{doc['id']}", headers=admin.headers,
                               json={"equipment_id": other_line})  # fmt: skip
    assert wrong.status_code == 422
    assert (
        await client.get(f"{API}/project-documents/{doc['id']}/file", headers=outsider.headers)
    ).status_code == 404
    assert (await client.get(f"{API}/projects/{pid}/documents", headers=outsider.headers)).status_code == 404

    renamed = await client.patch(f"{API}/project-documents/{doc['id']}", headers=admin.headers,
                                 json={"title": "GA drawing, P-101A", "revision": "Z2"})  # fmt: skip
    assert renamed.json()["title"] == "GA drawing, P-101A" and renamed.json()["revision"] == "Z2"
    gone = await client.delete(f"{API}/project-documents/{letter.json()['id']}", headers=admin.headers)
    assert gone.status_code == 204
    assert await titles() == ["GA drawing, P-101A", "Package schedule"]
    _ = other_pid


async def test_lead_time_and_cost_benchmarks(client: AsyncClient, factory: Factory) -> None:
    tag = uuid.uuid4().hex[:6]
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])  # sees commercial data
    viewer = await factory.user(tenant, ["viewer"])
    pid, line_id = await _project_with_line(client, admin.headers, tag)
    for weeks, amount in ((18, "100000"), (22, "120000"), (26, "140000"), (30, None)):
        r = await client.post(f"{API}/procurement-records", headers=admin.headers, json={
            "project_id": pid, "equipment_id": line_id, "vendor_name": f"Fjord {tag}", "po_number": f"PO-{weeks}",
            "lead_time_weeks": weeks, "amount": amount, "currency": "USD" if amount else None,
        })  # fmt: skip
        assert r.status_code == 201, r.text

    data = (await client.get(f"{API}/equipment/benchmarks", headers=admin.headers,
                             params={"category_code": "pumps"})).json()  # fmt: skip
    row = next(b for b in data["rows"] if b["key"] == BB2)
    assert row["records"] == 4 and row["lead_records"] == 4 and row["lead_median"] == 24.0
    assert row["lead_p25"] <= 22 <= row["lead_p80"] and row["expected"].startswith("Expect ")
    # Per unit: the line has 2 units, so 120,000 -> 60,000.
    assert [(p["currency"], p["records"], float(p["median"])) for p in row["prices"]] == [("USD", 3, 60000.0)]

    hidden = (await client.get(f"{API}/equipment/benchmarks", headers=viewer.headers)).json()
    assert hidden["commercial_hidden"] is True and all(b["prices"] == [] for b in hidden["rows"])
    by_vendor = (await client.get(f"{API}/equipment/benchmarks", headers=viewer.headers,
                                  params={"group_by": "vendor"})).json()  # fmt: skip
    assert [b["label"] for b in by_vendor["rows"]] == [f"Fjord {tag}"]
