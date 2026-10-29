import time
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from app.ai.models import AiUsageEvent
from app.ai.pricing import call_cost
from app.ai.prompts import HEALTH_CHECK
from app.ai.service import CallContext
from app.api.deps import ContainerDep, SessionDep, require
from app.domain.permissions import P
from app.domain.principal import Principal

router = APIRouter(tags=["ai"])


class PromptUsage(BaseModel):
    prompt: str
    calls: int
    cost_usd: Decimal


class AiUsageRead(BaseModel):
    configured: bool
    model: str | None
    month_start: datetime
    budget_usd: Decimal
    spent_usd: Decimal
    remaining_usd: Decimal
    calls: int
    by_prompt: list[PromptUsage]


class AiCheckResult(BaseModel):
    ok: bool
    provider: str
    model: str
    echo: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class _HealthCheck(BaseModel):
    ok: bool
    echo: str


@router.get("/ai/usage", response_model=AiUsageRead)
async def ai_usage(
    principal: Annotated[Principal, Depends(require(P.DISCOVERY_RUN))], session: SessionDep, c: ContainerDep
) -> AiUsageRead:
    """This month's AI spending for your organization against its budget (platform staff: the platform budget)."""
    budget = await c.ai.budget(principal.tenant_id)
    rows = await session.execute(
        select(AiUsageEvent.prompt, func.count(), func.coalesce(func.sum(AiUsageEvent.cost_usd), 0))
        .where(
            AiUsageEvent.tenant_id.is_not_distinct_from(principal.tenant_id),
            AiUsageEvent.created_at >= budget.month_start,
        )
        .group_by(AiUsageEvent.prompt)
        .order_by(func.sum(AiUsageEvent.cost_usd).desc())
    )
    return AiUsageRead(
        configured=c.ai.configured,
        model=c.ai.provider.model if c.ai.provider else None,
        month_start=budget.month_start,
        budget_usd=budget.limit_usd,
        spent_usd=budget.spent_usd,
        remaining_usd=budget.remaining_usd,
        calls=budget.calls,
        by_prompt=[PromptUsage(prompt=p, calls=n, cost_usd=Decimal(cost)) for p, n, cost in rows.all()],
    )


@router.post("/admin/ai/check", response_model=AiCheckResult)
async def check_ai(
    principal: Annotated[Principal, Depends(require(P.TENANT_MANAGE))], c: ContainerDep
) -> AiCheckResult:
    """Makes one tiny AI call (a fraction of a cent) to confirm the key, model and network work."""
    started = time.monotonic()
    result = await c.ai.run(
        HEALTH_CHECK, _HealthCheck, CallContext(None, principal.user_id), word="masteratlas"
    )
    return AiCheckResult(
        ok=result.output.ok and result.output.echo.strip().lower() == "masteratlas",
        provider=c.ai.provider.name if c.ai.provider else "none",
        model=result.usage.model,
        echo=result.output.echo,
        latency_ms=int((time.monotonic() - started) * 1000),
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost_usd=call_cost(result.usage, c.settings),
    )
