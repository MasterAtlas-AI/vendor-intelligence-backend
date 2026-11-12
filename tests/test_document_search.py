"""Project documents' text, read on the server: search across every project by the words of reports and letters
(SOW H.4 / 25.4), and a datasheet's ratings offered for its equipment line (SOW 3.11 / 3.12 / 8.4)."""

import uuid
from typing import Any

from httpx import AsyncClient

from tests.conftest import Factory
from tests.test_documents_benchmarks import _project_with_line

API = "/api/v1"


async def test_document_text_search_and_ratings(client: AsyncClient, factory: Factory) -> None:
    tag = uuid.uuid4().hex[:6]
    word = f"zq{tag}"  # a token the text search reads the same way in the file and the query
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    viewer = await factory.user(tenant, ["viewer"])
    outsider = await factory.user(await factory.tenant(), ["tenant_admin"])
    pid, line_id = await _project_with_line(client, admin.headers, tag)
    other_pid, _ = await _project_with_line(client, admin.headers, tag + "b")

    async def add(project: str, name: str, body: bytes, doc_type: str, **data: str) -> dict[str, Any]:
        r = await client.post(f"{API}/projects/{project}/documents", headers=admin.headers,
                              files={"file": (name, body, "application/octet-stream")},
                              data={"doc_type": doc_type, **data})  # fmt: skip
        assert r.status_code == 201, r.text
        return dict(r.json())

    letter = await add(pid, "letter.txt", (
        f"Ref {word}. The vendor confirms the mechanical seals need a plan 53B flushing system; "
        "delivery of the seal pots is delayed by six weeks."
    ).encode(), "correspondence", title="Seal supplier letter")  # fmt: skip
    assert letter["text_chars"] > 50
    sheet = await add(pid, "P-datasheet.csv", (
        b"Item,Seawater lift pump\nCapacity (m3/h),120\nDifferential head,85 m\nRated power,75 kW\nRev,B\n"
    ), "datasheet", equipment_id=line_id)  # fmt: skip
    report = await add(other_pid, "inspection.txt", f"{word} FAT witnessed; vibration within API 610 limits".encode(),
                       "report", title="FAT report")  # fmt: skip
    drawing = await add(pid, "GA.dwg", b"\x00\x01binary", "drawing")
    assert drawing["text_chars"] == 0

    async def search(headers: dict[str, str], q: str, **extra: str) -> dict[str, Any]:
        r = await client.get(f"{API}/project-documents/search", headers=headers, params={"q": q, **extra})
        assert r.status_code == 200, r.text
        return dict(r.json())

    # Words inside the files, across projects, stemmed ("flushing" finds "flush"), with the matching passage.
    found = await search(viewer.headers, f"{word} seal flush delayed")
    hits = found["items"]
    assert [h["id"] for h in hits] == [letter["id"]]
    assert "[[" in hits[0]["snippet"] and hits[0]["project_name"] == f"FPSO Docs {tag}"
    both = await search(viewer.headers, word)
    assert {h["id"] for h in both["items"]} >= {letter["id"], report["id"]}
    only = await search(viewer.headers, word, project_id=other_pid)
    assert [h["id"] for h in only["items"]] == [report["id"]]
    assert (await search(viewer.headers, word, doc_type="report"))["total"] == 1
    assert (await search(outsider.headers, word))["total"] == 0  # another organization sees none of it

    # The datasheet's ratings, beside the line's current (empty) values.
    rated = await client.get(f"{API}/project-documents/{sheet['id']}/ratings", headers=viewer.headers)
    assert rated.status_code == 200, rated.text
    body = rated.json()
    assert body["equipment_id"] == line_id and body["equipment_label"] == f"P-{tag} · Seawater lift pump"
    ratings = {r["key"]: r for r in body["ratings"]}
    assert ratings["capacity"]["found"] == 120 and ratings["capacity"]["current"] is None
    assert ratings["head"]["found"] == 85 and ratings["power"]["found"] == 75
    assert ratings["capacity"]["source"].startswith("Capacity (m3/h)")

    # Copying them is an ordinary equipment edit; the ratings then show as current.
    copied = await client.patch(f"{API}/equipment/{line_id}", headers=admin.headers, json={
        "version": body["equipment_version"], "attributes": {k: r["found"] for k, r in ratings.items()},
    })  # fmt: skip
    assert copied.status_code == 200, copied.text
    again = (
        await client.get(f"{API}/project-documents/{sheet['id']}/ratings", headers=viewer.headers)
    ).json()
    assert all(r["current"] == r["found"] for r in again["ratings"])
    hidden = await client.get(f"{API}/project-documents/{sheet['id']}/ratings", headers=outsider.headers)
    assert hidden.status_code == 404
