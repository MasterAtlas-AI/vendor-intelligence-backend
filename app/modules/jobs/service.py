import uuid
from typing import Any

import anyio
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, NotFound
from app.core.logging import get_logger
from app.core.pagination import paginate
from app.domain.enums import JobStatus, JobType
from app.domain.permissions import P
from app.domain.principal import Principal
from app.infra.queue import JobDispatcher
from app.modules.audit.service import record_audit
from app.modules.jobs.models import Job

log = get_logger(__name__)

# job type -> (celery task name, queue)
TASK_ROUTES: dict[str, tuple[str, str]] = {
    JobType.DIAGNOSTICS: ("jobs.diagnostics", "default"),
}


class JobService:
    def __init__(self, session: AsyncSession, principal: Principal, dispatcher: JobDispatcher) -> None:
        self.s = session
        self.principal = principal
        self.dispatcher = dispatcher

    async def _replay(self, job_type: JobType, idempotency_key: str) -> Job | None:
        """The job previously created with this key, if any. Keys are scoped to the caller."""
        existing = await self.s.scalar(
            select(Job).where(
                Job.tenant_id.is_not_distinct_from(self.principal.tenant_id),
                Job.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            return None
        if existing.type != job_type or existing.user_id != self.principal.user_id:
            raise Conflict(
                "Idempotency key already used for a different request", code="idempotency_conflict"
            )
        return existing

    async def create(self, job_type: JobType, payload: dict[str, Any], idempotency_key: str | None) -> Job:
        if idempotency_key and (existing := await self._replay(job_type, idempotency_key)):
            return existing

        task_name, queue = TASK_ROUTES[job_type]
        job = Job(
            tenant_id=self.principal.tenant_id,
            user_id=self.principal.user_id,
            type=job_type,
            input=payload,
            idempotency_key=idempotency_key,
            queue=queue,
        )
        self.s.add(job)
        try:
            await self.s.flush()
        except IntegrityError:
            # Lost a race with a concurrent request carrying the same Idempotency-Key.
            await self.s.rollback()
            if idempotency_key and (existing := await self._replay(job_type, idempotency_key)):
                return existing
            raise
        record_audit(
            self.s,
            "job.created",
            principal=self.principal,
            entity_type="job",
            entity_id=job.id,
            changes={"type": job_type},
        )
        # Commit before enqueueing so the worker can always see the row.
        await self.s.commit()
        try:
            job.celery_task_id = await anyio.to_thread.run_sync(
                self.dispatcher.dispatch, task_name, job.id, queue
            )
        except Exception as exc:
            log.exception("job.dispatch_failed", job_id=str(job.id))
            job.status = JobStatus.FAILED
            job.error = {"code": "dispatch_failed", "message": str(exc)[:500]}
        await self.s.commit()
        return job

    def _visible(self, stmt: Select[Job]) -> Select[Job]:
        stmt = stmt.where(Job.tenant_id.is_not_distinct_from(self.principal.tenant_id))
        if not self.principal.has(P.AUDIT_READ):
            stmt = stmt.where(Job.user_id == self.principal.user_id)
        return stmt

    async def list_page(
        self, *, job_type: str | None, status: JobStatus | None, limit: int, cursor: str | None
    ) -> tuple[list[Job], str | None]:
        stmt = self._visible(select(Job))
        if job_type:
            stmt = stmt.where(Job.type == job_type)
        if status:
            stmt = stmt.where(Job.status == status)
        rows, nxt = await paginate(
            self.s, stmt, created_at_col=Job.created_at, id_col=Job.id, limit=limit, cursor=cursor
        )
        return list(rows), nxt

    async def get(self, job_id: uuid.UUID) -> Job:
        job = await self.s.scalar(self._visible(select(Job).where(Job.id == job_id)))
        if job is None:
            raise NotFound("Job not found")
        return job

    async def cancel(self, job_id: uuid.UUID) -> Job:
        job = await self.get(job_id)
        if job.status.is_terminal:
            raise Conflict("Job already finished", code="job_finished")
        job.cancel_requested = True
        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.CANCELLED  # worker skips cancelled jobs when it picks them up
        record_audit(
            self.s, "job.cancel_requested", principal=self.principal, entity_type="job", entity_id=job.id
        )
        await self.s.commit()
        return job
