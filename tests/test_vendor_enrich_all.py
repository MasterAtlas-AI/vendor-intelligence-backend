"""AI on the whole knowledge base as one background job (quality/enrich_all.py), with a scripted AI: field by field,
answers that check out become proposals, live partial results, one run at a time, vendors already checked are
skipped next time, and the spending limit stops the run before it spends."""

from typing import Any

from httpx import AsyncClient
from sqlalchemy import update

from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.db.session import open_session
from app.domain.principal import TenantContext
from app.modules.vendors.models import Vendor
from tests.conftest import Factory
from tests.test_ai_lookup import ai_basics, tag  # noqa: F401  (fixtures)
from tests.test_imports import _drain
from tests.test_search import _vendor

API = "/api/v1"


async def _job(client: AsyncClient, headers: dict[str, str], job_id: str) -> dict[str, Any]:
    job: dict[str, Any] = (await client.get(f"{API}/jobs/{job_id}", headers=headers)).json()
    return job


async def test_enrich_all_runs_in_the_background_and_resumes(
    client: AsyncClient,
    factory: Factory,
    container: Container,
    ai_basics: FakeProvider,  # noqa: F811
    tag: str,  # noqa: F811
) -> None:
    oslo = await _vendor(container, f"Oslo Seal Works {tag}", "NO", [])
    myth = await _vendor(container, f"Myth Pumps {tag}", "NO", [])
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        await s.execute(
            update(Vendor).where(Vendor.id.in_([oslo, myth])).values(hq_country=None, description="")
        )
        await s.commit()
    curator = await factory.user(None, platform_roles=["curator"])
    viewer = await factory.user(await factory.tenant(), ["tenant_admin"])
    ids = [str(oslo), str(myth)]

    # What a run would do, and who may start one.
    preview = await client.get(f"{API}/admin/vendor-enrichment", headers=curator.headers)
    assert preview.status_code == 200, preview.text
    assert {"website", "hq_country", "description", "contacts"} <= set(preview.json()["planned"])
    assert preview.json()["budget_remaining_usd"] > 0
    assert (await client.get(f"{API}/admin/vendor-enrichment", headers=viewer.headers)).status_code == 403

    await _drain(container.job_runner)
    body = {"fields": ["description", "hq_country"], "max_usd": 5, "vendor_ids": ids}
    started = await client.post(f"{API}/admin/vendor-enrichment", headers=curator.headers, json=body)
    assert started.status_code == 202, started.text
    assert started.json()["planned"] == {"hq_country": 2, "description": 2}
    again = await client.post(f"{API}/admin/vendor-enrichment", headers=curator.headers, json=body)
    assert again.status_code == 409  # one run at a time

    await _drain(container.job_runner)
    job = await _job(client, curator.headers, started.json()["job_id"])
    assert job["status"] == "succeeded", job
    result = job["result"]
    assert result["fields"] == ["hq_country", "description"]  # headquarters before descriptions, as ordered
    assert result["checked"] == 4 and result["total"] == 4 and result["stopped"] is None
    assert result["parallel"] == container.settings.ai_enrich_parallel  # one pass, several look-ups at a time
    assert result["counts"]["hq_country"]["proposed"] == 1 and result["counts"]["hq_country"]["rejected"] == 1
    assert result["counts"]["description"]["proposed"] == 1
    rows = {(r["vendor_id"], r["field"]): r for r in result["rows"]}
    proposal = rows[(str(oslo), "hq_country")]["proposal_id"]
    assert proposal and rows[(str(oslo), "hq_country")]["value"] == "NO"
    assert (await client.get(f"{API}/admin/proposals/{proposal}", headers=curator.headers)).status_code == 200

    # The run counts as an AI check: a second run skips these vendors, so there is nothing left to do.
    second = await client.post(f"{API}/admin/vendor-enrichment", headers=curator.headers, json=body)
    assert second.status_code == 422 and "Nothing to look up" in second.text
    board = (await client.get(f"{API}/admin/data-quality", headers=curator.headers,
                              params={"q": f"Myth Pumps {tag}", "limit": 5})).json()  # fmt: skip
    checks = {c["field"] for c in board["worst"][0]["ai_checks"]}
    assert {"hq_country", "description"} <= checks

    # The spending limit is checked before each batch: a limit below one batch's estimate spends nothing.
    late = await _vendor(container, f"Late Valves {tag}", "NO", [])
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        await s.execute(update(Vendor).where(Vendor.id == late).values(hq_country=None))
        await s.commit()
    capped = await client.post(f"{API}/admin/vendor-enrichment", headers=curator.headers,
                               json={"fields": ["hq_country"], "max_usd": 0.001, "vendor_ids": [str(late)]})  # fmt: skip
    assert capped.status_code == 202, capped.text
    await _drain(container.job_runner)
    stopped = (await _job(client, curator.headers, capped.json()["job_id"]))["result"]
    assert stopped["stopped"] == "limit" and stopped["checked"] == 0 and stopped["spent_usd"] == 0
