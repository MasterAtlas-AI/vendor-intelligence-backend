import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, require
from app.core.pagination import CursorPage
from app.db.session import open_session
from app.domain.enums import JobStatus, JobType
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.jobs.schemas import DiagnosticsJobCreate, JobAccepted, JobRead
from app.modules.jobs.service import JobService

router = APIRouter(tags=["jobs"])

SSE_POLL_SECONDS = 1.0
SSE_HEARTBEAT_SECONDS = 15.0


def get_job_service(session: SessionDep, principal: PrincipalDep, c: ContainerDep) -> JobService:
    return JobService(session, principal, c.jobs)


Svc = Annotated[JobService, Depends(get_job_service)]


def _accepted(request: Request, job_id: uuid.UUID, job_status: JobStatus) -> JobAccepted:
    prefix = request.app.state.container.settings.api_prefix
    return JobAccepted(job_id=job_id, status=job_status, events_url=f"{prefix}/jobs/{job_id}/events")


@router.get("/jobs", response_model=CursorPage[JobRead])
async def list_jobs(
    svc: Svc,
    type_: Annotated[str | None, Query(alias="type", max_length=50)] = None,
    status_: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    cursor: str | None = None,
) -> CursorPage[JobRead]:
    rows, nxt = await svc.list_page(job_type=type_, status=status_, limit=limit, cursor=cursor)
    return CursorPage(
        items=[JobRead.model_validate(j) for j in rows], next_cursor=nxt, has_more=nxt is not None
    )


@router.get("/jobs/{job_id}", response_model=JobRead)
async def get_job(job_id: uuid.UUID, svc: Svc) -> JobRead:
    return JobRead.model_validate(await svc.get(job_id))


@router.post("/jobs/{job_id}/cancel", response_model=JobRead)
async def cancel_job(job_id: uuid.UUID, svc: Svc) -> JobRead:
    return JobRead.model_validate(await svc.cancel(job_id))


@router.get("/jobs/{job_id}/events", response_class=StreamingResponse)
async def job_events(
    job_id: uuid.UUID, request: Request, svc: Svc, principal: PrincipalDep, c: ContainerDep
) -> StreamingResponse:
    """Server-Sent Events: `progress` on every change, `completed`/`failed`/`cancelled` at the end.

    Polls the jobs table with short-lived sessions so no connection is held for the stream's lifetime.
    (Phase 3 swaps polling for Redis pub/sub without changing the event contract.)
    """
    await svc.get(job_id)  # 404 before opening the stream
    await svc.s.rollback()  # release the request session's connection; the stream uses its own sessions

    async def stream() -> AsyncIterator[str]:
        last: tuple[object, ...] | None = None
        idle = 0.0
        while not await request.is_disconnected():
            async with open_session(c.sessionmaker, principal.tenant_context(), principal.user_id) as s:
                job = await JobService(s, principal, c.jobs).get(job_id)
                snapshot = JobRead.model_validate(job)
            state = (snapshot.status, snapshot.stage, snapshot.progress)
            if state != last:
                last, idle = state, 0.0
                event = snapshot.status.value if snapshot.status.is_terminal else "progress"
                if snapshot.status == JobStatus.SUCCEEDED:
                    event = "completed"
                yield f"event: {event}\ndata: {snapshot.model_dump_json()}\n\n"
                if snapshot.status.is_terminal:
                    return
            elif idle >= SSE_HEARTBEAT_SECONDS:
                idle = 0.0
                yield f": heartbeat {json.dumps(str(job_id))}\n\n"
            await asyncio.sleep(SSE_POLL_SECONDS)
            idle += SSE_POLL_SECONDS

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/admin/jobs/diagnostics", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def start_diagnostics_job(
    body: DiagnosticsJobCreate,
    request: Request,
    _: Annotated[Principal, Depends(require(P.TENANT_MANAGE))],
    svc: Svc,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> JobAccepted:
    """Runs a no-op job through queue → worker → DB → SSE to verify the background pipeline end to end."""
    job = await svc.create(JobType.DIAGNOSTICS, body.model_dump(), idempotency_key)
    return _accepted(request, job.id, job.status)
