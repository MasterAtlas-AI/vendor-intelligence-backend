"""Postgres-backed job runner (no Redis, no Celery).

The `jobs` table is the queue. A runner — inside the API process by default, or standalone via
`python -m app.cli run-worker` — claims the oldest runnable job with `FOR UPDATE SKIP LOCKED`, so any number of
runners can share the table without double-processing. A claim is a lease (`locked_by`, `locked_until`) that
the runner renews while the handler works. If the process dies or is redeployed, the lease lapses and the job
is claimed again, up to `max_attempts`; after that it is marked failed. Handlers must therefore tolerate being
re-run from the start.

Handlers run on the API's event loop: CPU-heavy work (PDF parsing, large spreadsheets) must be offloaded with
`anyio.to_thread.run_sync` so requests stay responsive.
"""

import asyncio
import os
import socket
import traceback
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import open_session
from app.domain.enums import JobStatus
from app.domain.principal import TenantContext
from app.modules.jobs.models import Job

if TYPE_CHECKING:
    from app.ai.service import AiService

log = get_logger(__name__)


class JobCancelled(Exception):
    pass


class JobLeaseLost(Exception):
    """Another runner took the job over (our lease expired); stop without writing results."""


class JobContext:
    """Handed to job handlers: report progress, check cancellation, open tenant-scoped sessions."""

    def __init__(self, runner: "JobRunner", job: Job) -> None:
        self._runner = runner
        self.settings = runner.settings
        self.ai = runner.ai  # AiService (None only in runners built without one, e.g. some tests)
        self.job_id = job.id
        self.tenant_id = job.tenant_id
        self.user_id = job.user_id
        self.attempt = job.attempts
        self.input: dict[str, Any] = dict(job.input)

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """For helpers that open their own short sessions (e.g. the crawler's page cache)."""
        return self._runner.sessionmaker

    def session(self) -> AbstractAsyncContextManager[AsyncSession]:
        """A session confined to the job's tenant (platform jobs get platform mode)."""
        ctx = TenantContext(self.tenant_id) if self.tenant_id else TenantContext.platform()
        return open_session(self._runner.sessionmaker, ctx, self.user_id)

    async def progress(self, stage: str, progress: int) -> None:
        """Record progress (shown live via SSE). Raises JobCancelled if the user cancelled the job."""
        # FOR NO KEY UPDATE, not FOR UPDATE: the handler's own open transaction holds FOR KEY SHARE on this row
        # as soon as it inserts rows referencing the job (e.g. import_rows.job_id), and FOR UPDATE would wait on
        # it forever while the handler waits on us.
        async with open_session(self._runner.sessionmaker, TenantContext.platform()) as s:
            job = await s.get(Job, self.job_id, with_for_update={"key_share": True})
            if job is None or job.locked_by != self._runner.worker_id or job.status != JobStatus.RUNNING:
                raise JobLeaseLost
            if job.cancel_requested:
                raise JobCancelled
            job.stage = stage
            job.progress = max(0, min(100, progress))
            await s.commit()


Handler = Callable[[JobContext], Awaitable[dict[str, Any] | None]]

_REAP_SQL = text(
    """
    UPDATE jobs
       SET status = CASE WHEN cancel_requested THEN 'cancelled'::job_status ELSE 'failed'::job_status END,
           error = CASE WHEN cancel_requested THEN NULL ELSE jsonb_build_object(
                       'code', 'lease_expired',
                       'message', 'The worker stopped responding on all ' || attempts || ' attempt(s)') END,
           locked_by = NULL, locked_until = NULL, finished_at = now(), updated_at = now()
     WHERE status = 'running' AND locked_until < now()
       AND (attempts >= max_attempts OR cancel_requested)
    """
)

_CLAIM_SQL = text(
    """
    UPDATE jobs
       SET status = 'running', locked_by = :worker, locked_until = now() + make_interval(secs => :lease),
           attempts = attempts + 1, started_at = coalesce(started_at, now()), error = NULL, updated_at = now()
     WHERE id = (
            SELECT id FROM jobs
             WHERE NOT cancel_requested
               AND ((status = 'queued' AND run_after <= now())
                    OR (status = 'running' AND locked_until < now() AND attempts < max_attempts))
             ORDER BY created_at
             FOR UPDATE SKIP LOCKED
             LIMIT 1)
    RETURNING id
    """
)


class JobRunner:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        handlers: Mapping[str, Handler] | None = None,
        ai: "AiService | None" = None,
    ) -> None:
        from app.workers.handlers import HANDLERS  # local: handlers import feature modules

        self.sessionmaker = sessionmaker
        self.settings = settings
        self.ai = ai
        self._s = settings
        self._handlers = handlers if handlers is not None else HANDLERS
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._active: set[asyncio.Task[None]] = set()
        self._loops: list[asyncio.Task[None]] = []

    @property
    def running(self) -> bool:
        return bool(self._loops)

    def notify(self) -> None:
        """Wake the poll loop now (a job was just queued in this process)."""
        self._wake.set()

    # ------------------------------------------------------------------ lifecycle

    async def start(self, *, maintenance: bool = True) -> None:
        if self._loops:
            return
        self._stopping.clear()
        self._loops.append(asyncio.create_task(self._poll_loop(), name="job-runner-poll"))
        if maintenance:
            self._loops.append(asyncio.create_task(self._maintenance_loop(), name="job-runner-maintenance"))
        log.info("job_runner.started", worker_id=self.worker_id, concurrency=self._s.job_concurrency)

    async def stop(self, grace_seconds: float = 10.0) -> None:
        """Stop claiming, give running jobs a moment to finish, then cancel the rest (their leases
        lapse and another runner picks them up)."""
        if not self._loops:
            return
        self._stopping.set()
        self._wake.set()
        await asyncio.gather(*self._loops, return_exceptions=True)
        self._loops.clear()
        if self._active:
            _, pending = await asyncio.wait(self._active, timeout=grace_seconds)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        log.info("job_runner.stopped", worker_id=self.worker_id)

    async def _poll_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                while len(self._active) < self._s.job_concurrency and not self._stopping.is_set():
                    job_id = await self.claim_next()
                    if job_id is None:
                        break
                    task = asyncio.create_task(self.execute(job_id), name=f"job-{job_id}")
                    self._active.add(task)
                    task.add_done_callback(self._active.discard)
            except Exception:
                log.exception("job_runner.poll_failed")
            self._wake.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._s.job_poll_seconds)

    async def _maintenance_loop(self) -> None:
        from app.workers.maintenance import run_maintenance

        while not self._stopping.is_set():
            try:
                await run_maintenance(self.sessionmaker)
            except Exception:
                log.exception("maintenance.failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._s.maintenance_interval_minutes * 60
                )

    # ------------------------------------------------------------------ claiming and running

    async def claim_next(self) -> uuid.UUID | None:
        """Atomically claim one runnable job (or None). Also settles jobs whose retries are exhausted."""
        async with open_session(self.sessionmaker, TenantContext.platform()) as s:
            await s.execute(_REAP_SQL)
            job_id = (
                await s.execute(_CLAIM_SQL, {"worker": self.worker_id, "lease": self._s.job_lease_seconds})
            ).scalar()
            await s.commit()
        if job_id is not None:
            log.info("job.claimed", job_id=str(job_id), worker_id=self.worker_id)
        return job_id

    async def run_once(self) -> uuid.UUID | None:
        """Claim and run a single job inline. Used by tests and one-off CLI runs."""
        job_id = await self.claim_next()
        if job_id is not None:
            await self.execute(job_id)
        return job_id

    async def execute(self, job_id: uuid.UUID) -> None:
        async with open_session(self.sessionmaker, TenantContext.platform()) as s:
            job = await s.get(Job, job_id)
            if job is None or job.locked_by != self.worker_id:
                return
            ctx = JobContext(self, job)
            handler = self._handlers.get(job.type)

        heartbeat = asyncio.create_task(self._heartbeat(job_id))
        outcome: dict[str, Any]
        try:
            if handler is None:
                raise LookupError(f"No handler registered for job type {job.type!r}")
            result = await handler(ctx)
            outcome = {
                "status": JobStatus.SUCCEEDED,
                "result": result or {},
                "progress": 100,
                "stage": "done",
            }
        except JobLeaseLost:
            log.warning("job.lease_lost", job_id=str(job_id), worker_id=self.worker_id)
            return
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
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

        async with open_session(self.sessionmaker, TenantContext.platform()) as s:
            job = await s.get(Job, job_id, with_for_update={"key_share": True})
            if job is None or job.locked_by != self.worker_id or job.status != JobStatus.RUNNING:
                log.warning("job.result_discarded", job_id=str(job_id), reason="lease no longer held")
                return
            for key, value in outcome.items():
                setattr(job, key, value)
            job.locked_by = None
            job.locked_until = None
            job.finished_at = datetime.now(UTC)
            await s.commit()
        log.info("job.finished", job_id=str(job_id), status=outcome["status"])

    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        interval = max(1.0, self._s.job_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                async with open_session(self.sessionmaker, TenantContext.platform()) as s:
                    await s.execute(
                        update(Job)
                        .where(
                            Job.id == job_id, Job.locked_by == self.worker_id, Job.status == JobStatus.RUNNING
                        )
                        .values(locked_until=datetime.now(UTC) + timedelta(seconds=self._s.job_lease_seconds))
                    )
                    await s.commit()
            except Exception:
                log.exception("job.heartbeat_failed", job_id=str(job_id))
