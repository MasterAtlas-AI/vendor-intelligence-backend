"""SOW 7.4: classification society type-approval registers as a lookup. The search is held to the registers' own
domains; a certificate is proposed only with a register page as its source, never twice, and waits for review."""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient

from app.ai.ports import Citation
from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.domain.enums import ProposalItemAction
from app.modules.proposals.service import NewItem
from app.modules.quality.lookup import CLASS_REGISTERS, ClassApprovalsFinding
from tests.conftest import Factory
from tests.test_imports import _drain
from tests.test_search import _vendor

API = "/api/v1"


def _company(text: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(text.split("<<<DATA\n", 1)[1].split("\nDATA>>>", 1)[0])
    return data


@pytest.fixture
def registers(container: Container) -> Iterator[FakeProvider]:
    def answer(text: str) -> dict[str, Any]:
        name = _company(text)["name"]
        if name.startswith("Unknown"):
            return {"approvals": [], "confidence": 0.9, "reason": "Not on any register"}
        if name.startswith("Unsure"):
            return {"approvals": [{"society": "DNV", "certificate_number": "TAP000001", "product": "Pumps",
                                   "valid_to": None, "url": "https://approvalfinder.dnv.com/TAP000001"}],
                    "confidence": 0.5, "reason": "A similar name"}  # fmt: skip
        return {
            "approvals": [
                {"society": "DNV GL", "certificate_number": "TAP00002XY", "product": "Centrifugal pumps",
                 "valid_to": "2028-06-30", "url": "https://approvalfinder.dnv.com/certificate/TAP00002XY"},
                {"society": "ABS", "certificate_number": "21-HS1234567-PDA", "product": "Pump, centrifugal",
                 "valid_to": "2027-01-15", "url": None},  # sourced by the search's citation of eagle.org
                {"society": "BV", "certificate_number": "12345/A0 BV", "product": "Pumps",
                 "valid_to": "2026-12-01", "url": "https://some-directory.example/bv"},  # not a register page
                {"society": "LR", "certificate_number": "LR-KNOWN-1", "product": "Pumps", "valid_to": None,
                 "url": "https://www.lr.org/approvals/LR-KNOWN-1"},  # already on record
            ],
            "confidence": 0.9,
            "reason": "Manufacturer name and address match on DNV and ABS",
        }  # fmt: skip

    fake = FakeProvider(citations=[Citation(url="https://ww2.eagle.org/typeapproval/21-HS1234567-PDA")])
    fake.on(ClassApprovalsFinding, answer)
    previous, container.ai.provider = container.ai.provider, fake
    yield fake
    container.ai.provider = previous


async def test_class_register_lookup(
    client: AsyncClient, factory: Factory, container: Container, registers: FakeProvider
) -> None:
    tag = uuid.uuid4().hex[:6]
    known = await _vendor(container, f"Known Pumps {tag}", "NO", [
        NewItem(ProposalItemAction.ADD_CERTIFICATION, {"certification_code": "LR_TA", "certificate_number": "LR-KNOWN-1"}),
    ])  # fmt: skip
    unknown = await _vendor(container, f"Unknown Valves {tag}", "DE", [])
    unsure = await _vendor(container, f"Unsure Fans {tag}", "SG", [])
    curator = await factory.user(None, platform_roles=["curator"])

    await _drain(container.job_runner)
    started = await client.post(f"{API}/admin/data-quality/lookup", headers=curator.headers,
                                json={"vendor_ids": [str(known), str(unknown), str(unsure)], "field": "class_approvals"})  # fmt: skip
    assert started.status_code == 202, started.text
    await _drain(container.job_runner)
    job = (await client.get(f"{API}/jobs/{started.json()['job_id']}", headers=curator.headers)).json()
    assert job["status"] == "succeeded", job
    rows = {r["vendor_id"]: r for r in job["result"]["rows"]}

    # The search was held to the registers' domains.
    call = next(c for c in registers.calls if c.schema == "ClassApprovalsFinding")
    assert call.web_search and set(call.allowed_domains or []) == {d for _, d in CLASS_REGISTERS.values()}

    found = rows[str(known)]
    assert found["status"] == "proposed" and found["value"] == "2 type approval(s)"
    assert "1 without a register page left out" in found["reason"] and "1 already recorded" in found["reason"]
    proposal = (
        await client.get(f"{API}/admin/proposals/{found['proposal_id']}", headers=curator.headers)
    ).json()
    certs = {i["payload"]["certification_code"]: i for i in proposal["items"]}
    assert set(certs) == {"DNV_TA", "ABS_TA"}
    assert certs["DNV_TA"]["payload"]["certificate_number"] == "TAP00002XY"
    assert certs["DNV_TA"]["payload"]["valid_to"] == "2028-06-30"
    assert certs["ABS_TA"]["evidence"][0]["url"].startswith("https://ww2.eagle.org/")
    assert rows[str(unknown)]["status"] == "not_found"
    assert rows[str(unsure)]["status"] == "rejected" and rows[str(unsure)]["proposal_id"] is None

    # Approved, they are ordinary certification facts on the vendor.
    approved = await client.post(f"{API}/admin/proposals/{proposal['id']}/approve", headers=curator.headers,
                                 json={"version": proposal["version"]})  # fmt: skip
    assert approved.status_code == 200, approved.text
    detail = (await client.get(f"{API}/vendors/{known}", headers=curator.headers)).json()
    numbers = {c["certificate_number"] for c in detail["certifications"]}
    assert {"TAP00002XY", "21-HS1234567-PDA", "LR-KNOWN-1"} <= numbers
