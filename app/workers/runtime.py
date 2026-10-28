"""Bridges synchronous Celery tasks to the async DB layer and gives tasks a uniform job lifecycle."""

import asyncio
import traceback
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import build_engine, build_sessionmaker, open_session
from app.domain.enums import JobStatus
from app.domain.principal import TenantContext
from app.modules.jobs.models import Job

log = get_logger(__name__)


class JobCancelled(Exception):
    pass


class JobContext:
    """Handed to job handlers: report progress, check cancellation, open tenant-scoped sessions."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], job: Job) -> None:
        self._sm = sessionmaker
        self.job_id = job.id
        self.tenant_id = job.tenant_id
        self.user_id = job.user_id
        self.input: dict[str, Any] = dict(job.input)

    def session(self) -> Any:
        """A session confined to the job's tenant (platform jobs get platform mode)."""
        ctx = TenantContext(self.tenant_id) if self.tenant_id else TenantContext.platform()
        return open_session(self._sm, ctx, self.user_id)

    async def progress(self, stage: str, progress: int) -> None:
        async with open_session(self._sm, TenantContext.platform()) as s:
            job = await s.get(Job, self.job_id)
            assert job is not None
            if job.cancel_requested:
                raise JobCancelled
            job.stage = stage
            job.progress = max(0, min(100, progress))
            await s.commit()
        log.info("job.progress", job_id=str(self.job_id), stage=stage, progress=progress)


Handler = Callable[[JobContext], Awaitable[dict[str, Any] | None]]


async def execute_job(
    job_id: uuid.UUID, handler: Handler, sessionmaker: async_sessionmaker[AsyncSession] | None = None
) -> None:
    """Runs one job through its lifecycle. Workers pass no sessionmaker (a NullPool engine is created per task)."""
    engine = None if sessionmaker else build_engine(get_settings(), null_pool=True)
    sm = sessionmaker or build_sessionmaker(engine)  # type: ignore[arg-type]
    try:
        async with open_session(sm, TenantContext.platform()) as s:
            job = await s.get(Job, job_id, with_for_update=True)
            if job is None:
                log.warning("job.missing", job_id=str(job_id))
                return
            if job.status.is_terminal:
                log.info("job.skip_terminal", job_id=str(job_id), status=job.status)
                return
            job.status = JobStatus.RUNNING
            job.attempts += 1
            job.started_at = job.started_at or datetime.now(UTC)
            job.error = None
            await s.commit()
            ctx = JobContext(sm, job)

        outcome: dict[str, Any] = {}
        try:
            result = await handler(ctx)
            outcome = {
                "status": JobStatus.SUCCEEDED,
                "result": result or {},
                "progress": 100,
                "stage": "done",
            }
        except JobCancelled:
            outcome = {"status": JobStatus.CANCELLED, "stage": "cancelled"}
        except Exception as exc:
            log.exception("job.failed", job_id=str(job_id))
            outcome = {
                "status": JobStatus.FAILED,
                "error": {
                    "code": type(exc).__name__,
                    "message": str(exc)[:1000],
                    "trace": traceback.format_exc(limit=5)[-4000:],
                },
            }

        async with open_session(sm, TenantContext.platform()) as s:
            job = await s.get(Job, job_id)
            assert job is not None
            for key, value in outcome.items():
                setattr(job, key, value)
            job.finished_at = datetime.now(UTC)
            await s.commit()
        log.info("job.finished", job_id=str(job_id), status=outcome["status"])
    finally:
        if engine is not None:
            await engine.dispose()


async def mark_crashed(
    job_id: uuid.UUID, exc: BaseException, sessionmaker: async_sessionmaker[AsyncSession] | None = None
) -> None:
    """Best effort: a failure outside the handler (bad deploy, DB outage) must not leave a job 'queued' forever."""
    engine = None if sessionmaker else build_engine(get_settings(), null_pool=True)
    sm = sessionmaker or build_sessionmaker(engine)  # type: ignore[arg-type]
    try:
        async with open_session(sm, TenantContext.platform()) as s:
            job = await s.get(Job, job_id)
            if job is not None and not job.status.is_terminal:
                job.status = JobStatus.FAILED
                job.error = {"code": "worker_crash", "message": f"{type(exc).__name__}: {exc}"[:1000]}
                job.finished_at = datetime.now(UTC)
                await s.commit()
    finally:
        if engine is not None:
            await engine.dispose()


def run_job(job_id: str, handler: Handler) -> None:
    try:
        asyncio.run(execute_job(uuid.UUID(job_id), handler))
    except Exception as exc:
        log.exception("job.crashed", job_id=job_id)
        try:
            asyncio.run(mark_crashed(uuid.UUID(job_id), exc))
        except Exception:
            log.exception("job.mark_crashed_failed", job_id=job_id)
        raise
