"""Accuracy evaluation with the real AI and real websites (Phase 3 exit criteria). Spends money, so it only runs when
asked:

    RUN_REAL_AI=1 pytest tests/accuracy -s            # whole golden set (30 vendors, about US$1-3)
    RUN_REAL_AI=1 EVAL_LIMIT=5 pytest tests/accuracy -s

Uses the test database (never production) and the OpenAI key from .env. For each vendor in golden.json it runs
capability discovery ("Discover equipment") and scores the proposed equipment facts against the vendor's expected and
wrong taxonomy prefixes. A second test runs one open vendor discovery and lists the candidates for manual labelling.
Reports go to tests/accuracy/reports/ (gitignored).

Targets: fact precision >= 90%, vendor precision >= 85%, a 25-candidate discovery under 5 minutes and US$5.
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from app.ai.service import build_provider
from app.core.config import Settings
from app.core.container import Container
from app.db.session import open_session
from app.domain.enums import ProposalItemAction, ProposalKind, ProposalOrigin, ProposalStatus
from app.domain.normalization import registrable_domain
from app.domain.principal import TenantContext
from app.modules.imports.models import ImportRow
from app.modules.jobs.models import Job
from app.modules.proposals.models import KnowledgeProposal
from app.modules.proposals.service import NewItem, ProposalService
from app.modules.tenants.models import Tenant
from app.modules.vendors.models import VendorDomain
from tests.conftest import Factory
from tests.test_imports import _drain

API = "/api/v1"
HERE = Path(__file__).parent
REPORTS = HERE / "reports"
PARALLEL = 5

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_AI") != "1", reason="set RUN_REAL_AI=1 (costs money)"
)


def golden() -> list[dict[str, Any]]:
    vendors: list[dict[str, Any]] = json.loads((HERE / "golden.json").read_text(encoding="utf-8"))["vendors"]
    return vendors[: int(os.environ.get("EVAL_LIMIT", len(vendors)))]


def under(code: str, prefixes: list[str]) -> bool:
    return any(code == p or code.startswith(p + ".") for p in prefixes)


@dataclass
class Score:
    correct: list[str] = field(default_factory=list)
    wrong: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)  # neither expected nor wrong: judge by hand
    missed: list[str] = field(default_factory=list)  # 'must' prefixes with no fact
    error: str | None = None
    cost: float = 0.0
    seconds: float = 0.0


def score(vendor: dict[str, Any], codes: list[str]) -> Score:
    s = Score()
    for code in codes:
        if under(code, vendor["expected"]):
            s.correct.append(code)
        elif under(code, vendor["wrong"]):
            s.wrong.append(code)
        else:
            s.review.append(code)
    s.missed = [m for m in vendor["must"] if not any(under(c, [m]) for c in codes)]
    return s


@pytest.fixture
async def real_ai(container: Container) -> Any:
    provider = build_provider(Settings())  # the key from .env; the test settings deliberately have none
    if provider is None:
        pytest.skip("OPENAI_API_KEY is not configured")
    previous, container.ai.provider = container.ai.provider, provider
    yield provider
    container.ai.provider = previous


async def _eval_tenant(container: Container, factory: Factory) -> Any:
    """An organization with room in its AI budget (returns its id; make users with factory.user)."""
    tenant = await factory.tenant()
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        await s.execute(update(Tenant).where(Tenant.id == tenant).values(quotas={"ai_monthly_usd": 20}))
        await s.commit()
    return tenant


async def _vendor_for(container: Container, vendor: dict[str, Any]) -> uuid.UUID:
    """The golden vendor in the test database (created once), with earlier evaluation proposals rejected so this run
    proposes everything again."""
    domain = registrable_domain(vendor["website"])
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        vendor_id = await s.scalar(select(VendorDomain.vendor_id).where(VendorDomain.domain == domain))
        if vendor_id is None:
            p = await ProposalService(s, None).submit(
                ProposalKind.NEW_VENDOR, ProposalOrigin.MANUAL,
                [NewItem(ProposalItemAction.CREATE_VENDOR, {"legal_name": vendor["name"], "website": domain})],
                auto_approve=True,
            )  # fmt: skip
            vendor_id = p.subject_vendor_id
        else:
            await s.execute(
                update(KnowledgeProposal)
                .where(KnowledgeProposal.subject_vendor_id == vendor_id,
                       KnowledgeProposal.status == ProposalStatus.PENDING_REVIEW)
                .values(status=ProposalStatus.REJECTED)
            )  # fmt: skip
        await s.commit()
    assert vendor_id is not None
    return vendor_id


async def _run_job(
    client: AsyncClient, container: Container, user: Any, path: str, body: dict[str, Any]
) -> Any:
    await _drain(container.job_runner)
    started = await client.post(f"{API}{path}", json=body, headers=user.headers)
    assert started.status_code == 202, started.text
    await container.job_runner.run_once()
    return await _job_row(container, started.json()["job_id"])


async def _job_row(container: Container, job_id: str) -> dict[str, Any]:
    """The finished job straight from the database: a long run outlives the user's 15-minute access token."""
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        job = await s.get(Job, uuid.UUID(job_id))
        assert job is not None
        return {"id": str(job.id), "status": job.status.value, "result": job.result or {}, "error": job.error,
                "cost_usd": str(job.cost_usd)}  # fmt: skip


async def _wait(client: AsyncClient, user: Any, job_id: str) -> Any:
    while True:
        job = (await client.get(f"{API}/jobs/{job_id}", headers=user.headers)).json()
        if job["status"] in ("succeeded", "partially_succeeded", "failed", "cancelled"):
            return job
        await asyncio.sleep(2)


async def test_capability_accuracy(
    client: AsyncClient, factory: Factory, container: Container, real_ai: Any
) -> None:
    tenant = await _eval_tenant(container, factory)
    await _drain(container.job_runner)

    async def one(vendor: dict[str, Any]) -> tuple[dict[str, Any], Score, list[dict[str, Any]]]:
        vendor_id = await _vendor_for(container, vendor)
        # Fresh users per vendor: access tokens expire after 15 minutes and the whole set takes much longer.
        engineer = await factory.user(tenant, ["engineer"])
        curator = await factory.user(None, platform_roles=["curator"])
        started = time.monotonic()
        resp = await client.post(f"{API}/discovery/vendors/{vendor_id}/equipment", json={"max_pages": 12},
                                 headers=engineer.headers)  # fmt: skip
        assert resp.status_code == 202, resp.text
        await container.job_runner.run_once()  # runs some queued evaluation job (one per vendor started)
        job = await _wait(client, engineer, resp.json()["job_id"])
        facts: list[dict[str, Any]] = []
        result = job.get("result") or {}
        if job["status"] == "succeeded" and result.get("proposal_id"):
            proposal = (await client.get(f"{API}/admin/proposals/{result['proposal_id']}",
                                         headers=curator.headers)).json()  # fmt: skip
            facts = [i for i in proposal["items"] if i["action"] == "add_equipment"]
        s = score(vendor, [f["payload"]["category_code"] for f in facts])
        if job["status"] != "succeeded":
            s.error = str(job.get("error"))[:200]
        elif not facts:
            s.error = f"no equipment proposed ({result.get('pages_read', '?')} pages read)"
        s.cost, s.seconds = float(job.get("cost_usd") or 0), time.monotonic() - started
        print(f"{vendor['name']}: {len(s.correct)} ok, {len(s.wrong)} wrong, {len(s.review)} review, "
              f"missed {s.missed}, ${s.cost:.3f}, {s.seconds:.0f}s {s.error or ''}")  # fmt: skip
        return vendor, s, facts

    results: list[tuple[dict[str, Any], Score, list[dict[str, Any]]]] = []
    vendors = golden()
    for i in range(0, len(vendors), PARALLEL):  # a few vendors at a time (different sites, so still polite)
        results += await asyncio.gather(*(one(v) for v in vendors[i : i + PARALLEL]))

    report, precision = capability_report(results)
    path = write_report("capabilities", report)
    print(f"\nReport: {path}")
    assert precision is None or precision >= 0.90, f"fact precision {precision:.0%} is below 90%"


def capability_report(
    results: list[tuple[dict[str, Any], Score, list[dict[str, Any]]]],
) -> tuple[str, float | None]:
    correct = sum(len(s.correct) for _, s, _ in results)
    wrong = sum(len(s.wrong) for _, s, _ in results)
    review = sum(len(s.review) for _, s, _ in results)
    must = sum(len(v["must"]) for v, _, _ in results)
    found = must - sum(len(s.missed) for _, s, _ in results)
    precision = correct / (correct + wrong) if correct + wrong else None
    lines = [
        f"# Capability discovery accuracy, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        f"- Vendors: {len(results)} ({sum(1 for _, s, _ in results if s.error)} failed)",
        f"- Equipment facts proposed: {correct + wrong + review} (all with a verified quote from the vendor's site)",
        f"- Fact precision (scored facts): {precision:.1%}" if precision is not None else "- Fact precision: n/a",
        f"  - correct {correct}, wrong {wrong}, outside the key (review by hand) {review}",
        f"- Recall of must-find categories: {found}/{must}",
        f"- Cost: ${sum(s.cost for _, s, _ in results):.2f}, time: {sum(s.seconds for _, s, _ in results) / 60:.1f} min",
        "",
        "| Vendor | Correct | Wrong | Review | Missed | Cost | Error |",
        "|---|---|---|---|---|---|---|",
    ]  # fmt: skip
    for v, s, _ in results:
        lines.append(f"| {v['name']} | {len(s.correct)} | {', '.join(s.wrong)} | {', '.join(s.review)} | "
                     f"{', '.join(s.missed)} | ${s.cost:.3f} | {s.error or ''} |")  # fmt: skip
    lines += ["", "## Facts to review by hand", ""]
    for v, s, facts in results:
        for f in facts:
            if f["payload"]["category_code"] in s.review + s.wrong:
                quote = (f["evidence"][0]["snippet"] if f["evidence"] else "").replace("\n", " ")[:200]
                lines.append(f"- **{v['name']}** `{f['payload']['category_code']}` {f['payload'].get('name')}: "
                             f"\"{quote}\" ({f['evidence'][0]['url'] if f['evidence'] else 'no url'})")  # fmt: skip
    return "\n".join(lines) + "\n", precision


BRIEF: dict[str, Any] = {
    "equipment_categories": ["pumps.centrifugal.api610.bb2"],
    "industries": ["fpso"],
    "brief": "Between-bearings API 610 pumps for FPSO water injection service",
    "max_candidates": 25,
    "pages_per_vendor": 8,
}


async def test_discovery_run(
    client: AsyncClient, factory: Factory, container: Container, real_ai: Any
) -> None:
    tenant = await _eval_tenant(container, factory)
    engineer = await factory.user(tenant, ["engineer"])
    estimate = (await client.post(f"{API}/discovery/estimate", json=BRIEF, headers=engineer.headers)).json()
    started = time.monotonic()
    job = await _run_job(client, container, engineer, "/discovery/vendors", BRIEF)
    minutes = (time.monotonic() - started) / 60
    assert job["status"] == "succeeded", job
    # Straight from the database: results are visible to the run's own user only, whose token may have expired.
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        rows: list[dict[str, Any]] = [
            {"outcome": r.outcome.value, "vendor_name": r.vendor_name, "details": r.details, "messages": r.messages}
            for r in await s.scalars(select(ImportRow).where(ImportRow.job_id == uuid.UUID(job["id"]))
                                     .order_by(ImportRow.row_number))
        ]  # fmt: skip
    cost = float(job["cost_usd"] or 0)
    lines = [
        f"# Vendor discovery run, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        f"Brief: {BRIEF['brief']} ({', '.join(BRIEF['equipment_categories'])})",
        "",
        f"- Time: {minutes:.1f} min (target < 5), cost: ${cost:.2f} (target < $5; "
        f"estimate ${estimate.get('typical_cost_usd', '?')}, max ${estimate.get('max_cost_usd', '?')})",
        f"- Stage start times (s): {job['result'].get('stage_started_s')}",
        f"- Candidates: {len(rows)}, proposed or matched: {sum(1 for r in rows if r['outcome'] != 'discarded')}",
        "",
        "Fill in the Label column (yes = a genuine maker/supplier for this brief, no = not) to get vendor precision.",
        "",
        "| Outcome | Vendor | Website | Equipment found | Why discarded | Label |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        d = r.get("details") or {}
        lines.append(f"| {r['outcome']} | {r['vendor_name']} | {d.get('website') or ''} | "
                     f"{', '.join((d.get('highlights') or {}).get('equipment', []))} | "
                     f"{'; '.join(r.get('messages') or [])[:120]} | |")  # fmt: skip
    path = write_report("discovery", "\n".join(lines) + "\n")
    print(f"\n{minutes:.1f} min, ${cost:.2f}. Report: {path}")
    assert minutes < 5 and cost < 5


def write_report(kind: str, text: str) -> Path:
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{kind}-{datetime.now(UTC):%Y%m%d-%H%M}.md"
    path.write_text(text, encoding="utf-8")
    return path
