"""The one way the application calls AI: budget check, retries with backoff, and metering of every billed call.

Usage is written in its own short transaction, so it is kept even when the job that made the call fails or rolls back,
and each job's running `cost_usd` is updated as it goes (visible live on the job page).
"""

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.models import AiUsageEvent
from app.ai.ports import AIError, AINotConfigured, AIRetryable, LLMProvider, LLMResult, Usage
from app.ai.pricing import call_cost
from app.ai.prompts import Prompt
from app.core.config import Settings
from app.core.exceptions import AppError, problem_response
from app.core.logging import get_logger
from app.db.session import open_session
from app.domain.principal import TenantContext
from app.modules.jobs.models import Job
from app.modules.tenants.models import Tenant

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

BUDGET_QUOTA_KEY = "ai_monthly_usd"


class BudgetExceeded(AppError):
    status_code, code, title = 402, "ai_budget_exceeded", "AI budget used up"


class AINotAvailable(AppError):
    status_code, code, title = 503, "ai_not_configured", "AI is not configured"


@dataclass(frozen=True)
class CallContext:
    """Who pays for a call."""

    tenant_id: uuid.UUID | None
    user_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Budget:
    month_start: datetime
    limit_usd: Decimal
    spent_usd: Decimal
    calls: int

    @property
    def remaining_usd(self) -> Decimal:
        return max(self.limit_usd - self.spent_usd, Decimal(0))


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def with_retries(
    call: Callable[[], Awaitable[Any]],
    *,
    attempts: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, AIRetryable], None] | None = None,
) -> Any:
    """Runs `call`, retrying AIRetryable with exponential backoff and jitter (1s, 2s, 4s, ... capped at 30s)."""
    for attempt in range(1, attempts + 2):
        try:
            return await call()
        except AIRetryable as exc:
            if attempt > attempts:
                raise
            if on_retry:
                on_retry(attempt, exc)
            await sleep(min(2 ** (attempt - 1), 30) * (1 + random.random() / 4))  # noqa: S311 - jitter
    raise AssertionError("unreachable")  # pragma: no cover


class AiService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        provider: LLMProvider | None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.provider = provider
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return self.provider is not None

    # ------------------------------------------------------------------ budget

    async def budget(self, tenant_id: uuid.UUID | None) -> Budget:
        start = month_start()
        async with open_session(self.sessionmaker, TenantContext.platform()) as s:
            limit = Decimal(str(self.settings.ai_platform_monthly_budget_usd))
            if tenant_id is not None:
                quotas = await s.scalar(select(Tenant.quotas).where(Tenant.id == tenant_id)) or {}
                limit = Decimal(
                    str(quotas.get(BUDGET_QUOTA_KEY, self.settings.ai_default_monthly_budget_usd))
                )
            spent, calls = (
                await s.execute(
                    select(func.coalesce(func.sum(AiUsageEvent.cost_usd), 0), func.count()).where(
                        AiUsageEvent.tenant_id.is_not_distinct_from(tenant_id),
                        AiUsageEvent.created_at >= start,
                    )
                )
            ).one()
        return Budget(month_start=start, limit_usd=limit, spent_usd=Decimal(spent), calls=int(calls))

    async def ensure_budget(self, ctx: CallContext, needed_usd: Decimal = Decimal(0)) -> Budget:
        budget = await self.budget(ctx.tenant_id)
        if budget.remaining_usd <= 0 or budget.remaining_usd < needed_usd:
            raise BudgetExceeded(
                f"This month's AI budget is used up (US${budget.spent_usd:.2f} of US${budget.limit_usd:.2f}). "
                "It resets on the 1st, or a platform admin can raise it."
            )
        return budget

    # ------------------------------------------------------------------ calls

    async def run(
        self,
        prompt: Prompt,
        schema: type[T],
        ctx: CallContext,
        *,
        web_search: bool = False,
        allowed_domains: list[str] | None = None,
        max_output_tokens: int | None = None,
        **inputs: Any,
    ) -> LLMResult[T]:
        provider = self.provider
        if provider is None:
            raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
        await self.ensure_budget(ctx)

        def retrying(attempt: int, exc: AIRetryable) -> None:
            log.warning("ai.retry", prompt=prompt.key, attempt=attempt, error=str(exc)[:200])

        async def call() -> LLMResult[T]:
            return await provider.structured(
                instructions=prompt.instructions,
                input=prompt.render(**inputs),
                schema=schema,
                web_search=web_search,
                allowed_domains=allowed_domains,
                max_output_tokens=max_output_tokens,
            )

        try:
            result: LLMResult[T] = await with_retries(
                call, attempts=self.settings.ai_max_retries, sleep=self._sleep, on_retry=retrying
            )
        except AINotConfigured as exc:
            raise AINotAvailable(str(exc)) from exc
        except AIError as exc:
            if exc.usage is not None:
                await self.record(prompt, exc.usage, ctx, outcome="error")
            raise
        await self.record(prompt, result.usage, ctx)
        return result

    async def record(self, prompt: Prompt, usage: Usage, ctx: CallContext, outcome: str = "ok") -> Decimal:
        cost = call_cost(usage, self.settings)
        session_ctx = TenantContext(ctx.tenant_id) if ctx.tenant_id else TenantContext.platform()
        async with open_session(self.sessionmaker, session_ctx, ctx.user_id) as s:
            s.add(
                AiUsageEvent(
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    job_id=ctx.job_id,
                    prompt=prompt.key,
                    provider=getattr(self.provider, "name", "unknown"),
                    model=usage.model,
                    input_tokens=usage.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    output_tokens=usage.output_tokens,
                    web_search_calls=usage.web_search_calls,
                    cost_usd=cost,
                    outcome=outcome,
                    created_at=datetime.now(UTC),
                )
            )
            if ctx.job_id:
                await s.execute(update(Job).where(Job.id == ctx.job_id).values(cost_usd=Job.cost_usd + cost))
            await s.commit()
        log.info(
            "ai.call",
            prompt=prompt.key,
            model=usage.model,
            cost_usd=str(cost),
            outcome=outcome,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            searches=usage.web_search_calls,
        )
        return cost


def build_provider(settings: Settings) -> LLMProvider | None:
    """The configured provider, or None (AI features then answer 503 'not configured' instead of crashing)."""
    if settings.ai_provider == "disabled" or settings.openai_api_key is None:
        return None
    from app.ai.providers.openai_provider import OpenAIProvider

    return OpenAIProvider(settings)


def register_ai_error_handler(app: FastAPI) -> None:
    """A failed AI call (refusal, unusable output, provider error after retries) is a 502 with a clear message,
    not an unhandled 500. Budget and configuration problems are AppErrors and keep their own status codes."""

    @app.exception_handler(AIError)
    async def _ai_error(request: Request, exc: AIError) -> JSONResponse:
        log.warning("ai.request_failed", path=request.url.path, error=str(exc)[:300])
        return problem_response(
            status=502,
            code="ai_failed",
            title="The AI service couldn't complete this request",
            detail=str(exc)[:500],
            instance=request.url.path,
        )
