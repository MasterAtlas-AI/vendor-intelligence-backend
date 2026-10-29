"""AI service against the database with the fake provider: metering, job cost, monthly budgets, endpoints."""

from collections.abc import Iterator
from decimal import Decimal

import pytest
from httpx import AsyncClient
from pydantic import BaseModel
from sqlalchemy import select, update

from app.ai.models import AiUsageEvent
from app.ai.ports import AIError, Usage
from app.ai.prompts import HEALTH_CHECK
from app.ai.providers.fake import FakeProvider
from app.ai.service import BudgetExceeded, CallContext
from app.core.container import Container
from app.db.session import open_session
from app.domain.enums import JobStatus, JobType
from app.domain.principal import TenantContext
from app.modules.ai.router import _HealthCheck
from app.modules.jobs.models import Job
from app.modules.tenants.models import Tenant
from tests.conftest import Factory


class Answer(BaseModel):
    ok: bool
    echo: str


@pytest.fixture
def fake_ai(container: Container) -> Iterator[FakeProvider]:
    fake = FakeProvider(usage=Usage(model="fake-model", input_tokens=1_000_000, output_tokens=0))
    fake.on(Answer, lambda text: {"ok": True, "echo": "masteratlas"})
    fake.on(_HealthCheck, lambda text: {"ok": True, "echo": "masteratlas"})  # the check endpoint's schema
    previous = container.ai.provider
    container.ai.provider = fake
    yield fake
    container.ai.provider = previous


async def _set_budget(container: Container, tenant_id: object, usd: float) -> None:
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        await s.execute(update(Tenant).where(Tenant.id == tenant_id).values(quotas={"ai_monthly_usd": usd}))
        await s.commit()


async def test_calls_are_metered_per_tenant_and_job(
    container: Container, factory: Factory, fake_ai: FakeProvider
) -> None:
    tenant = await factory.tenant()
    async with open_session(container.sessionmaker, TenantContext(tenant)) as s:
        # Already finished, so no job runner in another test ever picks it up; it only collects costs.
        job = Job(tenant_id=tenant, type=JobType.VENDOR_DISCOVERY, input={}, status=JobStatus.SUCCEEDED)
        s.add(job)
        await s.commit()

    ai = container.ai
    ctx = CallContext(tenant, job_id=job.id)
    result = await ai.run(HEALTH_CHECK, Answer, ctx, word="x")
    assert result.output.ok and fake_ai.calls[-1].input.endswith("DATA>>>")
    # 1M input tokens at the configured input price
    expected = Decimal(str(container.settings.ai_price_input_per_mtok)).quantize(Decimal("0.000001"))

    budget = await ai.budget(tenant)
    assert budget.spent_usd == expected and budget.calls == 1
    assert budget.limit_usd == Decimal(str(container.settings.ai_default_monthly_budget_usd))
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        assert (await s.get(Job, job.id)).cost_usd == expected  # type: ignore[union-attr]
        event = await s.scalar(select(AiUsageEvent).where(AiUsageEvent.job_id == job.id))
        assert event is not None and event.prompt == "health_check@v1" and event.outcome == "ok"

    # A billed call that produced unusable output is metered as an error.
    fake_ai.on(Answer, lambda text: AIError("bad output", Usage(model="fake-model", output_tokens=10)))
    with pytest.raises(AIError):
        await ai.run(HEALTH_CHECK, Answer, ctx, word="x")
    assert (await ai.budget(tenant)).calls == 2

    # Other tenants don't see or pay for it.
    other = await factory.tenant()
    assert (await ai.budget(other)).calls == 0


async def test_budget_stops_calls_once_used_up(
    container: Container, factory: Factory, fake_ai: FakeProvider
) -> None:
    tenant = await factory.tenant()
    price = container.settings.ai_price_input_per_mtok
    await _set_budget(container, tenant, price * 1.5)  # room for one call, not two
    ctx = CallContext(tenant)
    await container.ai.run(HEALTH_CHECK, Answer, ctx, word="x")
    await container.ai.run(
        HEALTH_CHECK, Answer, ctx, word="x"
    )  # starts while budget remains, then overshoots
    with pytest.raises(BudgetExceeded):
        await container.ai.run(HEALTH_CHECK, Answer, ctx, word="x")
    assert len(fake_ai.calls) == 2


async def test_usage_and_check_endpoints(
    client: AsyncClient, factory: Factory, container: Container, fake_ai: FakeProvider
) -> None:
    tenant = await factory.tenant()
    engineer = await factory.user(tenant, ["engineer"])
    viewer = await factory.user(tenant, ["viewer"])
    admin = await factory.user(None, platform_roles=["super_admin"])

    assert (await client.post("/api/v1/admin/ai/check", headers=engineer.headers)).status_code == 403
    check = await client.post("/api/v1/admin/ai/check", headers=admin.headers)
    assert check.status_code == 200, check.text
    assert check.json()["ok"] is True and check.json()["provider"] == "fake"

    await container.ai.run(HEALTH_CHECK, Answer, CallContext(tenant), word="x")
    usage = await client.get("/api/v1/ai/usage", headers=engineer.headers)
    assert usage.status_code == 200, usage.text
    u = usage.json()
    assert u["configured"] is True and u["calls"] == 1
    assert u["by_prompt"] == [{"prompt": "health_check@v1", "calls": 1, "cost_usd": u["spent_usd"]}]
    assert (await client.get("/api/v1/ai/usage", headers=viewer.headers)).status_code == 403

    # The admin's check was billed to the platform, not to the tenant.
    platform = (await client.get("/api/v1/ai/usage", headers=admin.headers)).json()
    assert platform["calls"] >= 1 and Decimal(platform["budget_usd"]) == Decimal(
        str(container.settings.ai_platform_monthly_budget_usd)
    )


async def test_not_configured_answers_503(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    previous, container.ai.provider = container.ai.provider, None
    try:
        admin = await factory.user(None, platform_roles=["super_admin"])
        resp = await client.post("/api/v1/admin/ai/check", headers=admin.headers)
        assert resp.status_code == 503 and resp.json()["code"] == "ai_not_configured"
    finally:
        container.ai.provider = previous


async def test_ai_failure_is_a_clean_502(
    client: AsyncClient, factory: Factory, container: Container, fake_ai: FakeProvider
) -> None:
    fake_ai.on(_HealthCheck, lambda text: AIError("The model declined: no"))
    admin = await factory.user(None, platform_roles=["super_admin"])
    resp = await client.post("/api/v1/admin/ai/check", headers=admin.headers)
    assert (
        resp.status_code == 502 and resp.json()["code"] == "ai_failed" and "declined" in resp.json()["detail"]
    )
