"""Celery task entrypoints. Each task is a thin shim over an async handler run by `run_job`."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text

from app.core.config import get_settings
from app.db.session import build_engine, build_sessionmaker, open_session
from app.domain.principal import TenantContext
from app.modules.auth.models import RefreshToken
from app.modules.users.models import UserToken
from app.workers.celery_app import celery_app
from app.workers.runtime import JobContext, run_job


async def diagnostics_handler(ctx: JobContext) -> dict[str, Any]:
    steps = int(ctx.input.get("steps", 3))
    delay = float(ctx.input.get("step_seconds", 1.0))
    fail_at = ctx.input.get("fail_at_step")
    for step in range(1, steps + 1):
        if fail_at == step:
            raise RuntimeError(f"Diagnostics failure requested at step {step}")
        await asyncio.sleep(delay)
        await ctx.progress(f"step_{step}", int(step * 100 / steps))
    async with ctx.session() as s:
        db_time = await s.scalar(select(func.now()))
    return {"steps": steps, "db_time": db_time.isoformat() if db_time else None}


@celery_app.task(name="jobs.diagnostics")
def diagnostics(job_id: str) -> None:
    run_job(job_id, diagnostics_handler)


async def _maintenance(fn: Any) -> None:
    engine = build_engine(get_settings(), null_pool=True)
    try:
        async with open_session(build_sessionmaker(engine), TenantContext.platform()) as s:
            await fn(s)
            await s.commit()
    finally:
        await engine.dispose()


@celery_app.task(name="maintenance.ensure_audit_partitions")
def ensure_audit_partitions() -> None:
    async def _do(s: Any) -> None:
        await s.execute(
            text(
                "SELECT ensure_audit_partition((now() + (n || ' month')::interval)::date) FROM generate_series(0, 2) n"
            )
        )

    asyncio.run(_maintenance(_do))


@celery_app.task(name="maintenance.purge_expired_tokens")
def purge_expired_tokens() -> None:
    cutoff = datetime.now(UTC) - timedelta(days=7)

    async def _do(s: Any) -> None:
        await s.execute(delete(RefreshToken).where(RefreshToken.expires_at < cutoff))
        await s.execute(delete(UserToken).where(UserToken.expires_at < cutoff))

    asyncio.run(_maintenance(_do))
