"""Assistant grounding check with the real AI (opt-in: RUN_REAL_AI=1, about US$0.03 for 10 questions).

A small fixed catalogue is created in the test database; each question is asked in a fresh conversation. Checked
automatically: the model cites no refs outside the records it was given (removed_refs == 0), every cited vendor is
one of the catalogue's (or another real knowledge-base vendor it looked up), and questions whose answer isn't in the
records don't get a made-up "yes". The answers go to tests/accuracy/reports/ for reading.
"""

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient

from app.ai.service import build_provider
from app.core.config import Settings
from app.core.container import Container
from app.domain.enums import ProposalItemAction
from app.modules.proposals.service import NewItem
from tests.accuracy.test_accuracy import REPORTS
from tests.conftest import Factory
from tests.test_search import _eq, _vendor

API = "/api/v1"
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_AI") != "1", reason="set RUN_REAL_AI=1 (costs money)"
)

# question, words that must NOT appear as a claim (checked case-insensitively), what a good answer does
QUESTIONS: list[tuple[str, list[str], str]] = [
    ("Who makes API 610 BB2 pumps for FPSO water injection?", [], "names Nordic BB2 Pumps"),
    ("Which vendors supply trunnion ball valves to API 6D?", [], "names Adriatic Valves"),
    (
        "Does Nordic BB2 Pumps hold ISO 14001?",
        ["holds iso 14001", "has iso 14001", "is iso 14001 certified"],
        "says the records don't show ISO 14001",
    ),
    ("What is the price of the HX-2 pump?", ["usd", "eur", "$"], "says pricing isn't in the records"),
    ("Compare Nordic BB2 Pumps and Adriatic Valves", [], "compares from the records only"),
    ("Who supplies LNG cryogenic pumps?", [], "says none in the records / suggests discovery"),
    ("Which of our vendors are approved?", [], "uses AVL status from the records"),
    ("Tell me about Gulf Compression Skids", [], "describes the compression packages"),
    ("Do any vendors hold NORSOK approval?", [], "names Nordic BB2 Pumps (NORSOK)"),
    ("What's the weather in Oslo?", [], "says it only helps with vendors"),
]


@pytest.fixture
async def real_ai(container: Container) -> Any:
    provider = build_provider(Settings())
    if provider is None:
        pytest.skip("OPENAI_API_KEY is not configured")
    previous, container.ai.provider = container.ai.provider, provider
    yield provider
    container.ai.provider = previous


async def test_assistant_grounding(
    client: AsyncClient, factory: Factory, container: Container, real_ai: Any
) -> None:
    tag = uuid.uuid4().hex[:4]
    nordic = await _vendor(container, f"Nordic BB2 Pumps {tag}", "NO", [
        _eq("pumps.centrifugal.api610.bb2", "HX-2 barrel pump", model_series="HX-2", standards=["API 610"]),
        NewItem(ProposalItemAction.ADD_CERTIFICATION, {"certification_code": "NORSOK"}),
        NewItem(ProposalItemAction.ADD_INDUSTRY, {"industry_code": "fpso"}),
    ], description="Norwegian maker of between-bearings pumps for water injection on FPSOs.")  # fmt: skip
    adriatic = await _vendor(container, f"Adriatic Valves {tag}", "IT", [
        _eq("valves.ball", "Trunnion-mounted ball valves", standards=["API 6D"]),
        NewItem(ProposalItemAction.ADD_CERTIFICATION, {"certification_code": "API_6D"}),
    ])  # fmt: skip
    gulf = await _vendor(container, f"Gulf Compression Skids {tag}", "AE", [
        _eq("packages.gas_compression", "Reciprocating gas compression packages"),
    ])  # fmt: skip
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    await client.put(f"{API}/my-vendors/{nordic}", json={"avl_status": "approved"}, headers=admin.headers)
    catalogue = {str(nordic), str(adriatic), str(gulf)}

    lines = [f"# Assistant grounding check, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC", ""]
    problems: list[str] = []
    for i, (question, forbidden, expect) in enumerate(QUESTIONS):
        if i % 4 == 0:
            admin = await factory.user(tenant, ["tenant_admin"])  # fresh token on slow runs
        q = (
            question.replace("Nordic BB2 Pumps", f"Nordic BB2 Pumps {tag}")
            .replace("Adriatic Valves", f"Adriatic Valves {tag}")
            .replace("Gulf Compression Skids", f"Gulf Compression Skids {tag}")
        )
        resp = await client.post(f"{API}/assistant/chat", headers=admin.headers,
                                 json={"messages": [{"role": "user", "content": q}]})  # fmt: skip
        assert resp.status_code == 200, (q, resp.text)
        body = resp.json()
        cited = {v["vendor_id"] for v in body["vendors"]}
        issues = []
        if body["removed_refs"]:
            issues.append(f"{body['removed_refs']} invented citation(s) removed")
        if outside := cited - catalogue:
            issues.append(f"cited {len(outside)} vendor(s) outside the test catalogue (real KB vendors?)")
        lowered = body["answer"].lower()
        if hits := [w for w in forbidden if w in lowered]:
            issues.append(f"possible unsupported claim: {hits}")
        if issues:
            problems.append(f"{question}: {'; '.join(issues)}")
        lines += [f"## {question}", f"_Expected: {expect}_", "", body["answer"], "",
                  f"Cited: {', '.join(v['name'] for v in body['vendors']) or 'none'} · actions: "
                  f"{', '.join(a['label'] for a in body['actions']) or 'none'} · checks: {'; '.join(issues) or 'ok'}",
                  ""]  # fmt: skip

    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"assistant-{datetime.now(UTC):%Y%m%d-%H%M}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {path}\nProblems: {problems or 'none'}")
    assert not [p for p in problems if "invented" in p or "unsupported" in p], problems
