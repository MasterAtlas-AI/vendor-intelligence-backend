"""The organization's own project history (SOW review changes 1-11): MEL import and review, equipment search and
filters, near-fit with labelled estimates, procurement and commercial hiding, vendor history, privacy between
organizations, Word tables, taxonomy admin, custom roles, AI controls and the new vendor-search filters."""

import io
import uuid
import zipfile
from typing import Any

from httpx import AsyncClient

from app.ai.ports import Usage
from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.modules.assistant.schemas import AssistantAnswer, AssistantPlan
from app.modules.equipment.extract import extract_spec_attributes
from app.modules.equipment.units import parse_quantity
from app.modules.imports.docx import read_docx_tables
from tests.conftest import Factory
from tests.test_search import _eq, _vendor

API = "/api/v1"
BB2 = "pumps.centrifugal.api610.bb2"


async def _drain(container: Container) -> None:
    while await container.job_runner.run_once():
        pass


def _docx(rows: list[list[str]]) -> bytes:
    cells = "".join(
        "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>"
        for row in rows
    )
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>Master Equipment List</w:t></w:r></w:p><w:tbl>{cells}</w:tbl></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_units_and_spec_extraction() -> None:
    assert parse_quantity("Capacities 20-1,200 m3/h", "flow") == [20.0, 1200.0]
    assert parse_quantity("650 m", "head") == [650.0, 650.0]
    assert parse_quantity("5 MW", "power") == [5000.0, 5000.0]
    assert parse_quantity("1800 m3/h", "head") is None  # m3/h is not a head
    assert parse_quantity("212 F", "temperature") == [100.0, 100.0]
    assert extract_spec_attributes(
        {"Max head": "650 m", "Capacities": "20-1,200 m3/h", "Overview": "pump"}
    ) == {
        "head": [650.0, 650.0],
        "capacity": [20.0, 1200.0],
    }
    tables = read_docx_tables(_docx([["Tag", "Description"], ["P-101", "Seawater lift pump"]]), 100)
    assert tables[0][1] == [["Tag", "Description"], ["P-101", "Seawater lift pump"]]


async def test_mel_import_review_search_and_privacy(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    pumps = await _vendor(
        container, f"Fjord Lift Pumps {tag}", "NO", [_eq(BB2, "SWL-2", standards=["API 610"])]
    )
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    viewer = await factory.user(tenant, ["viewer"])
    outsider = await factory.user(await factory.tenant(), ["tenant_admin"])

    project = await client.post(f"{API}/projects", headers=admin.headers, json={
        "name": f"FPSO Alpha {tag}", "kind": "historical", "year": 2021, "location": "offshore",
        "facility_type": "FPSO", "country": "MY",
    })  # fmt: skip
    assert project.status_code == 201, project.text
    pid = project.json()["id"]
    assert project.json()["kind"] == "historical"

    mel = (
        "Tag No,Equipment Description,Type,Vendor,Capacity (m3/h),Design Pressure,Power (kW),Weight,PO No\n"
        f"P-101A,Seawater lift pump,BB2 pump,Fjord Lift Pumps {tag},1200,15 barg,850,12 t,PO-{tag}-1\n"
        f"P-102A,Seawater lift pump,BB2 pump,Fjord Lift Pumps {tag},1800,15 barg,1200,16 t,\n"
        "V-201,Unknown gizmo,,Nobody Ltd,,,,,\n"
    ).encode()
    up = await client.post(f"{API}/projects/{pid}/mel", headers=admin.headers,
                           files={"file": ("mel.csv", mel, "text/csv")})  # fmt: skip
    assert up.status_code == 201, up.text
    sheet = up.json()["sheets"][0]
    mapping = sheet["suggested_mapping"]
    assert {"tag", "name", "category", "vendor", "attr:capacity", "attr:design_pressure", "attr:power",
            "attr:weight", "po_number"} <= set(mapping)  # fmt: skip
    upload_id = up.json()["upload_id"]
    parsed = await client.post(
        f"{API}/mel/{upload_id}/parse", headers=admin.headers, json={"project_id": pid}
    )
    assert parsed.status_code == 202, parsed.text
    await _drain(container)

    rows = (await client.get(f"{API}/mel/{upload_id}/rows", headers=admin.headers)).json()
    assert rows["counts"] == {"pending": 3}
    first, _, unknown = rows["rows"]
    assert first["parsed"]["category_code"] == BB2 and first["parsed"]["vendor_id"] == str(pumps)
    assert first["parsed"]["attributes"] == {"capacity": 1200.0, "design_pressure": 15.0, "power": 850.0,
                                             "weight": 12.0}  # fmt: skip
    assert any("pick a category" in i for i in unknown["issues"])
    assert any("Nobody Ltd" in i for i in unknown["issues"])

    # The project lists its MELs, so a review can be picked up from any browser.
    mels = (await client.get(f"{API}/projects/{pid}/mel", headers=viewer.headers)).json()
    assert [(m["upload_id"], m["filename"], m["job_id"], m["job_status"], m["counts"]) for m in mels] == [
        (upload_id, "mel.csv", parsed.json()["job_id"], "succeeded", {"pending": 3})
    ]

    # Nobody outside the organization sees the MEL; a viewer can't approve it.
    assert (await client.get(f"{API}/projects/{pid}/mel", headers=outsider.headers)).status_code == 404
    assert (await client.get(f"{API}/mel/{upload_id}/rows", headers=outsider.headers)).status_code == 404
    deny = await client.post(f"{API}/mel/rows/decide", headers=viewer.headers,
                             json={"row_ids": [first["id"]], "decision": "approve"})  # fmt: skip
    assert deny.status_code == 403

    fixed = await client.patch(f"{API}/mel/rows/{unknown['id']}", headers=admin.headers,
                               json={"category_code": "static.pressure_vessels"})  # fmt: skip
    assert fixed.status_code == 200 and not any("category" in i for i in fixed.json()["issues"])
    decided = await client.post(f"{API}/mel/rows/decide", headers=admin.headers, json={
        "row_ids": [r["id"] for r in rows["rows"]], "decision": "approve",
    })  # fmt: skip
    assert decided.json() == {"approved": 3, "rejected": 0, "skipped": 0, "vendors_linked": 1}

    # Search with filters: category, project year, location, attribute range.
    search = await client.post(f"{API}/equipment/search", headers=viewer.headers, json={
        "category_code": "pumps", "location": "offshore", "year_from": 2020, "ranges": {"capacity": [1500, None]},
    })  # fmt: skip
    assert search.status_code == 200, search.text
    assert [e["tag"] for e in search.json()["items"]] == ["P-102A"]
    assert search.json()["items"][0]["verification"] == "historical"
    assert (await client.post(f"{API}/equipment/search", headers=outsider.headers, json={})).json()[
        "total"
    ] == 0

    # Equipment status and service on a line (SOW 14.8, 4.17); filters by status, service and vendor/OEM (16.2).
    line = search.json()["items"][0]
    patched = await client.patch(f"{API}/equipment/{line['id']}", headers=admin.headers, json={
        "version": line["version"], "status": "in_service", "service": "Seawater injection",
    })  # fmt: skip
    assert patched.status_code == 200 and patched.json()["status"] == "in_service", patched.text
    bad = await client.patch(f"{API}/equipment/{line['id']}", headers=admin.headers,
                             json={"version": patched.json()["version"], "status": "scrapped"})  # fmt: skip
    assert bad.status_code == 422
    for filters, tags in (
        ({"statuses": ["in_service"]}, ["P-102A"]),
        ({"service": "seawater"}, ["P-102A"]),
        ({"vendor_id": str(pumps)}, ["P-101A", "P-102A"]),
        ({"statuses": ["decommissioned"]}, []),
    ):
        found = await client.post(f"{API}/equipment/search", headers=viewer.headers, json=filters)
        assert sorted(e["tag"] for e in found.json()["items"]) == tags, filters

    # Procurement from the PO column; amounts hidden without commercial:read.
    records = (await client.get(f"{API}/procurement-records?vendor_id={pumps}", headers=admin.headers)).json()
    assert [r["po_number"] for r in records] == [f"PO-{tag}-1"]
    put = await client.put(f"{API}/procurement-records/{records[0]['id']}", headers=admin.headers, json={
        "project_id": pid, "vendor_id": str(pumps), "po_number": f"PO-{tag}-1", "po_date": "2020-01-06",
        "promised_date": "2020-06-01", "delivered_date": "2020-05-25", "amount": "250000", "currency": "usd",
        "status": "delivered", "performance_rating": 4,
    })  # fmt: skip
    assert put.status_code == 200 and put.json()["on_time"] is True and put.json()["lead_time_weeks"] == 20
    seen = (await client.get(f"{API}/procurement-records?vendor_id={pumps}", headers=viewer.headers)).json()[
        0
    ]
    assert seen["amount"] is None and seen["commercial_hidden"] is True

    history = (await client.get(f"{API}/vendors/{pumps}/history", headers=viewer.headers)).json()
    assert [p["id"] for p in history["projects"]] == [pid] and history["equipment_count"] == 2
    assert history["on_time_rate"] == 1.0 and history["avg_rating"] == 4.0

    note = await client.post(f"{API}/knowledge-notes", headers=viewer.headers, json={
        "kind": "lesson_learned", "title": "Seal flush plan", "body": "Use plan 53B offshore.", "vendor_id": str(pumps),
        "verification": "verified",
    })  # fmt: skip
    assert note.status_code == 201 and note.json()["verification"] == "user_entered"  # only admins verify
    for kind in (
        "inspection",
        "project_update",
    ):  # QA/QC records and project-management notes (SOW 3.15, 10.2)
        made = await client.post(f"{API}/knowledge-notes", headers=viewer.headers, json={
            "kind": kind, "title": f"{kind} record", "body": "FAT witnessed; punch list closed.", "project_id": pid,
        })  # fmt: skip
        assert made.status_code == 201 and made.json()["kind"] == kind, made.text

    # Near-fit: the 1200 m3/h line is exact, the 1800 one comparable; weight at 1500 is interpolated, labelled.
    similar = await client.post(f"{API}/equipment/similar", headers=viewer.headers, json={
        "category_code": BB2, "attributes": {"capacity": 1200}, "estimate": ["weight"], "include_catalogue": False,
    })  # fmt: skip
    body = similar.json()
    assert [(m["match"], m["source"]) for m in body["matches"][:2]] == [("exact", "spe_history"),
                                                                        ("comparable", "spe_history")]  # fmt: skip
    assert "not approved engineering decisions" in body["notice"]
    near = await client.post(f"{API}/equipment/similar", headers=viewer.headers, json={
        "category_code": BB2, "attributes": {"capacity": 1500}, "estimate": ["weight"], "include_catalogue": False,
    })  # fmt: skip
    est = near.json()["estimates"][0]
    assert est["method"] == "interpolated" and est["value"] == 14.0 and "not verified" in est["label_note"]
    # The same duty scores higher (SOW 13.1): a crude-oil request ranks these seawater pumps lower.
    sea = await client.post(f"{API}/equipment/similar", headers=viewer.headers, json={
        "category_code": BB2, "service": "sea water", "attributes": {"capacity": 1200}, "include_catalogue": False,
    })  # fmt: skip
    crude = await client.post(f"{API}/equipment/similar", headers=viewer.headers, json={
        "category_code": BB2, "service": "crude oil", "attributes": {"capacity": 1200}, "include_catalogue": False,
    })  # fmt: skip
    assert sea.json()["matches"][0]["score"] > crude.json()["matches"][0]["score"]

    # Vendor search: only vendors the organization has used.
    used = await client.post(f"{API}/search", headers=viewer.headers, json={
        "query": f"pumps {tag}", "filters": {"used_by_us": True},
    })  # fmt: skip
    assert [h["vendor"]["id"] for h in used.json()["results"]] == [str(pumps)]

    tree = (await client.get(f"{API}/categories/browse", headers=viewer.headers)).json()
    pumps_node = next(n for n in tree if n["code"] == "pumps")
    assert pumps_node["equipment_count"] == 2 and pumps_node["vendor_count"] >= 1


async def test_word_mel_attributes_and_saved_searches(client: AsyncClient, factory: Factory) -> None:
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["data_admin"])
    pid = (await client.post(f"{API}/projects", headers=(await factory.user(tenant, ["tenant_admin"])).headers,
                             json={"name": "Topsides B", "kind": "historical"})).json()["id"]  # fmt: skip
    doc = _docx([["Tag", "Description", "Hull material"], ["T-301", "Atmospheric drain tank", "316L"]])
    custom = await client.post(f"{API}/equipment-attributes", headers=admin.headers,
                               json={"key": "hull_material", "label": "Hull material"})  # fmt: skip
    assert custom.status_code == 201 and custom.json()["shared"] is False
    up = await client.post(f"{API}/projects/{pid}/mel", headers=admin.headers, files={
        "file": ("mel.docx", doc, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    })  # fmt: skip
    assert up.status_code == 201, up.text
    assert up.json()["sheets"][0]["suggested_mapping"]["attr:hull_material"] == 2

    saved = await client.post(f"{API}/saved-searches", headers=admin.headers,
                              json={"kind": "equipment", "name": "Drain tanks", "filters": {"q": "drain"}})  # fmt: skip
    assert saved.status_code == 201
    assert [s["name"] for s in (await client.get(f"{API}/saved-searches", headers=admin.headers)).json()] == [
        "Drain tanks"
    ]


async def test_taxonomy_admin_and_custom_roles(client: AsyncClient, factory: Factory) -> None:
    curator = await factory.user(None, platform_roles=["curator"])
    key = f"t{uuid.uuid4().hex[:6]}"
    made = await client.post(f"{API}/taxonomy/equipment-categories", headers=curator.headers,
                             json={"parent_code": "telecom", "key": key, "name": "Test radios"})  # fmt: skip
    assert made.status_code == 201, made.text
    assert made.json()["code"] == f"telecom.{key}" and made.json()["level"] == 1
    # Platform staff outside an organization browse the tree too (without organization counts).
    tree = await client.get(f"{API}/categories/browse", headers=curator.headers)
    assert tree.status_code == 200 and all(n["equipment_count"] == 0 for n in tree.json())
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    denied = await client.post(f"{API}/taxonomy/equipment-categories", headers=admin.headers,
                               json={"key": "x" + key, "name": "Nope"})  # fmt: skip
    assert denied.status_code == 403

    role = await client.post(f"{API}/roles", headers=admin.headers, json={
        "name": "BD analyst", "permissions": ["vendor:read", "search:run", "equipment:read"],
    })  # fmt: skip
    assert role.status_code == 201, role.text
    assert (await client.post(f"{API}/roles", headers=admin.headers, json={
        "name": "Sneaky", "permissions": ["tenant:manage"]})).status_code == 422  # fmt: skip
    me = await client.patch(
        f"{API}/users/{admin.user_id}", headers=admin.headers, json={"department": "procurement"}
    )
    assert me.status_code == 200 and me.json()["department"] == "procurement"


async def test_assistant_respects_ai_controls(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    vid = await _vendor(container, f"Nordic Seal Pumps {tag}", "NO", [_eq(BB2, "NS-2")])
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    avl = await client.put(f"{API}/my-vendors/{vid}", headers=admin.headers, json={"avl_status": "approved"})
    assert avl.status_code == 200, avl.text
    pid = (await client.post(f"{API}/projects", headers=admin.headers,
                             json={"name": "Old FPSO", "kind": "historical"})).json()["id"]  # fmt: skip
    await client.post(f"{API}/equipment", headers=admin.headers, json={
        "project_id": pid, "name": "Booster pump", "category_code": BB2, "vendor_id": str(vid),
    })  # fmt: skip
    seen: list[str] = []

    def answer(text: str) -> dict[str, Any]:
        seen.append(text)
        return {"answer": "Nordic Seal Pumps [V1].", "cited_refs": ["V1"], "web_sources": [], "follow_ups": [],
                "actions": []}  # fmt: skip

    fake = FakeProvider(usage=Usage(model="fake-model"))
    fake.on(AssistantPlan, lambda _: {"intent": "find_vendors", "search_queries": [f"seal pumps {tag}"],
                                      "vendor_names": [], "my_vendors_only": False})  # fmt: skip
    fake.on(AssistantAnswer, answer)
    previous, container.ai.provider = container.ai.provider, fake
    try:
        ask = {"messages": [{"role": "user", "content": "BB2 pumps?"}]}
        first = (await client.post(f"{API}/assistant/chat", headers=admin.headers, json=ask)).json()
        row = first["shortlist"][0]
        assert (row["our_projects"], row["scores"]["our_experience"]) == (1, 0.333)
        assert "our_history" not in seen[0] and '"avl_status"' not in seen[0]  # private data stays local
        assert (
            first["private_data_shared"] is False and "not approved engineering decisions" in first["notice"]
        )
        # The organization's own matching equipment is listed beside the answer, but not sent to the AI.
        assert [m["name"] for m in first["history_matches"]] == ["Booster pump"]
        assert "Booster pump" not in seen[0]

        controls = await client.patch(f"{API}/tenant", headers=admin.headers, json={"ai": {
            "share_private_data": True, "web_sources": "approved_only", "approved_domains": ["https://www.api.org/x"],
        }})  # fmt: skip
        assert controls.status_code == 200, controls.text
        assert controls.json()["ai"]["approved_domains"] == ["api.org"]
        second = (await client.post(f"{API}/assistant/chat", headers=admin.headers, json=ask)).json()
        assert "our_history" in seen[1] and '"avl_status": "approved"' in seen[1]
        assert "Booster pump" in seen[1]  # shared once the organization allows it
        assert second["web_limited_to"] == ["api.org"]
        assert [c.allowed_domains for c in fake.calls if c.schema == "AssistantAnswer"][-1] == ["api.org"]
    finally:
        container.ai.provider = previous


async def test_bid_register_parent_company_agents_and_sources(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    from app.domain.enums import ProposalItemAction as A
    from app.modules.proposals.service import NewItem

    tag = uuid.uuid4().hex[:6]
    vid = await _vendor(container, f"Kestrel Valves {tag}", "GB", [
        _eq("valves.ball", "Trunnion ball valve"),
        NewItem(A.UPDATE_VENDOR_FIELD, {"field": "parent_company", "value": f"Kestrel Group {tag}"}),
        NewItem(A.ADD_CONTACT, {"contact_type": "agent", "label": "Authorised agent: Apex Engineering (India)",
                                "email": "sales@apex-maitest.in", "country": "IN"}),
    ])  # fmt: skip
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    detail = (await client.get(f"{API}/vendors/{vid}", headers=admin.headers)).json()
    assert detail["parent_company"] == f"Kestrel Group {tag}"
    assert [c["contact_type"] for c in detail["contacts"]] == ["agent"]

    pid = (await client.post(f"{API}/projects", headers=admin.headers,
                             json={"name": "Topsides C", "kind": "historical"})).json()["id"]  # fmt: skip
    register = (
        "Item,Description,Type,Vendor,Quoted price,Currency,Lead time,TBE result\n"
        f"1,Ball valve 10in,Ball valve,Kestrel Valves {tag},12500,EUR,18 weeks,Technically acceptable\n"
        "2,Ball valve 10in,Ball valve,Other Valves Ltd,9800,EUR,30 weeks,Not acceptable\n"
    ).encode()
    up = (await client.post(f"{API}/projects/{pid}/mel", headers=admin.headers,
                            files={"file": ("tbe.csv", register, "text/csv")})).json()  # fmt: skip
    assert {"amount", "currency", "lead_time", "technical_status"} <= set(
        up["sheets"][0]["suggested_mapping"]
    )
    await client.post(f"{API}/mel/{up['upload_id']}/parse", headers=admin.headers, json={"project_id": pid})
    await _drain(container)
    rows = (await client.get(f"{API}/mel/{up['upload_id']}/rows", headers=admin.headers)).json()["rows"]
    assert [r["parsed"]["technical_status"] for r in rows] == ["accepted", "rejected"]
    await client.post(f"{API}/mel/rows/decide", headers=admin.headers,
                      json={"row_ids": [r["id"] for r in rows], "decision": "approve"})  # fmt: skip

    quotes = (await client.get(f"{API}/procurement-records?vendor_id={vid}", headers=admin.headers)).json()
    assert [(q["record_type"], q["technical_status"], q["amount"], q["currency"], q["lead_time_weeks"])
            for q in quotes] == [("quotation", "accepted", "12500.00", "EUR", 18)]  # fmt: skip
    history = (await client.get(f"{API}/vendors/{vid}/history", headers=admin.headers)).json()
    assert (history["quotations"], history["technically_accepted"], history["purchase_orders"]) == (1, 1, 0)

    accepted = await client.post(f"{API}/search", headers=admin.headers, json={
        "query": f"valves {tag}", "filters": {"technically_accepted_by_us": True},
    })  # fmt: skip
    assert [h["vendor"]["id"] for h in accepted.json()["results"]] == [str(vid)]

    line = (await client.post(f"{API}/equipment/search", headers=admin.headers,
                              json={"project_ids": [pid], "q": "ball valve"})).json()["items"][0]  # fmt: skip
    assert line["has_source_file"] is True
    source = await client.get(f"{API}/equipment/{line['id']}/source", headers=admin.headers)
    assert source.status_code == 200 and source.content == register
