"""PDF import end to end: upload -> candidate facts with evidence -> job -> proposal for the chosen vendor."""

import uuid
from typing import Any

from httpx import AsyncClient

from app.core.container import Container
from tests.conftest import Actor, Factory
from tests.pdf_factory import make_pdf
from tests.test_imports import _drain, _existing_vendor, _upload, _vendor

API = "/api/v1"


def brochure(company: str, domain: str) -> bytes:
    return make_pdf(
        [
            [company, f"www.{domain}  sales@{domain}", "API 610 BB2 and BB5 barrel pumps for FPSO and LNG service."],
            [f"ISO 9001:2015 certified, Certificate No. QA-77-{domain[:4].upper()} valid until 31.03.2027."],
        ]
    )  # fmt: skip


async def _upload_pdf(client: AsyncClient, who: Actor, data: bytes) -> Any:
    resp = await client.post(
        f"{API}/imports/uploads",
        files={"file": ("brochure.pdf", data, "application/pdf")},
        headers=who.headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _run_pdf(
    client: AsyncClient, container: Container, who: Actor, body: dict[str, Any]
) -> dict[str, Any]:
    await _drain(container.job_runner)
    started = await client.post(f"{API}/imports/pdf", json=body, headers=who.headers)
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]
    assert await container.job_runner.run_once() == uuid.UUID(job_id)
    job = (await client.get(f"{API}/jobs/{job_id}", headers=who.headers)).json()
    assert job["status"] == "succeeded", job
    return dict(job)


async def test_pdf_import_proposes_new_vendor_and_enriches_existing(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    tenant = await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])
    domain = f"nordic-{tag}-maitest.com"
    company = f"Nordic {tag.upper()} Flow AS"

    up = await _upload_pdf(client, engineer, brochure(company, domain))
    assert up["kind"] == "pdf" and up["sheets"] == []
    info = up["pdf"]
    assert info["page_count"] == 2 and not info["scanned"] and info["facts_found"] >= 5
    assert info["suggested_domains"][0] == domain and info["suggested_names"][0] == company

    vendor = {"name": company, "website": domain, "country": "Norway"}
    preview = await client.post(
        f"{API}/imports/pdf/preview", json={"upload_id": up["id"], "vendor": vendor}, headers=engineer.headers
    )
    assert preview.status_code == 200, preview.text
    p = preview.json()
    keys = {c["key"] for c in p["candidates"]}
    assert {"equipment:pumps.centrifugal.api610.bb2", "certifications:ISO_9001", "industries:lng"} <= keys
    iso = next(c for c in p["candidates"] if c["key"] == "certifications:ISO_9001")
    assert iso["page"] == 2 and iso["details"]["valid_to"] == "2027-03-31"
    assert p["vendor"]["outcome"] == "proposed"

    # New vendor: everything but LNG goes into one new-vendor proposal, with page-level evidence.
    include = sorted(keys - {"industries:lng"})
    job = await _run_pdf(
        client, container, engineer, {"upload_id": up["id"], "vendor": vendor, "include": include}
    )
    result = job["result"]
    assert result["outcome"] == "proposed" and result["facts_selected"] == len(include)
    curator = await factory.user(None, platform_roles=["curator"])
    proposal = (
        await client.get(f"{API}/admin/proposals/{result['proposal_id']}", headers=curator.headers)
    ).json()
    assert proposal["origin"] == "pdf" and proposal["origin_tenant_id"] == str(tenant)
    actions = [i["action"] for i in proposal["items"]]
    assert actions[0] == "create_vendor" and "add_industry" in actions  # FPSO
    assert all(i["payload"].get("industry_code") != "lng" for i in proposal["items"])
    cert = next(i for i in proposal["items"] if i["payload"].get("certification_code") == "ISO_9001")
    assert (
        cert["payload"]["certificate_number"].startswith("QA-77-")
        and cert["payload"]["valid_to"] == "2027-03-31"
    )
    assert cert["evidence"][0]["kind"] == "document" and cert["evidence"][0]["page"] == 2
    assert "ISO 9001" in cert["evidence"][0]["snippet"] and cert["confidence"] is not None
    rows = (await client.get(f"{API}/imports/{job['id']}/rows", headers=engineer.headers)).json()
    assert rows["counts"] == {"proposed": 1} and rows["items"][0]["vendor_name"] == company

    # Existing vendor picked explicitly: added to the tenant's list, facts proposed as an update.
    existing = await _existing_vendor(container, f"Existing {tag} Pumps Ltd", f"existing-{tag}-maitest.com")
    job = await _run_pdf(
        client, container, engineer,
        {"upload_id": up["id"], "vendor": {"vendor_id": str(existing)}, "options": {"tags": ["brochure"]}},
    )  # fmt: skip
    assert job["result"]["outcome"] == "matched" and job["result"]["vendor_id"] == str(existing)
    update = (
        await client.get(f"{API}/admin/proposals/{job['result']['proposal_id']}", headers=curator.headers)
    ).json()
    assert update["kind"] == "update_vendor" and update["subject_vendor_id"] == str(existing)
    assert "create_vendor" not in [i["action"] for i in update["items"]]
    assert (await _vendor(client, engineer, existing))["tenant"]["tags"] == ["brochure"]


async def test_pdf_import_validation(client: AsyncClient, factory: Factory) -> None:
    tenant = await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])
    broken = await client.post(
        f"{API}/imports/uploads", files={"file": ("x.pdf", b"%PDF-1.4 nope", "application/pdf")},
        headers=engineer.headers,
    )  # fmt: skip
    assert broken.status_code == 422 and "PDF" in broken.json()["detail"]

    pdf = await _upload_pdf(client, engineer, brochure("Acme AS", "acme-maitest.com"))
    sheet = (await _upload(client, engineer, [["Vendor", "Website"], ["Acme", ""]])).json()

    no_vendor = await client.post(
        f"{API}/imports/pdf", json={"upload_id": pdf["id"]}, headers=engineer.headers
    )
    assert no_vendor.status_code == 422
    wrong_kind = await client.post(
        f"{API}/imports/excel/preview",
        json={"upload_id": pdf["id"], "mapping": {"name": 0}},
        headers=engineer.headers,
    )
    assert wrong_kind.status_code == 422 and "spreadsheet" in wrong_kind.json()["detail"]
    wrong_kind = await client.post(
        f"{API}/imports/pdf/preview", json={"upload_id": sheet["id"]}, headers=engineer.headers
    )
    assert wrong_kind.status_code == 422 and "PDF" in wrong_kind.json()["detail"]
    unknown_vendor = await client.post(
        f"{API}/imports/pdf", json={"upload_id": pdf["id"], "vendor": {"vendor_id": str(uuid.uuid4())}},
        headers=engineer.headers,
    )  # fmt: skip
    assert unknown_vendor.status_code == 404
