import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, UploadFile, status
from fastapi.responses import Response

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, require
from app.core.exceptions import ValidationFailed
from app.domain.enums import ImportRowOutcome
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.imports.schemas import (
    ImportPreview,
    ImportRequest,
    ImportRowPage,
    ImportRowRead,
    UploadRead,
)
from app.modules.imports.service import ImportService
from app.modules.jobs.schemas import JobAccepted

router = APIRouter(tags=["imports"])
Uploader = Annotated[Principal, Depends(require(P.UPLOAD_CREATE))]


def get_service(session: SessionDep, principal: PrincipalDep, c: ContainerDep) -> ImportService:
    return ImportService(session, principal, c.settings)


Svc = Annotated[ImportService, Depends(get_service)]

CHUNK = 1024 * 1024


@router.post("/imports/uploads", response_model=UploadRead, status_code=status.HTTP_201_CREATED)
async def upload_file(file: UploadFile, _: Uploader, svc: Svc, c: ContainerDep) -> UploadRead:
    """Stores a vendor list (.xlsx or .csv) and returns its sheets, detected headers, sample rows and a
    suggested column mapping. Nothing is imported yet."""
    limit = c.settings.upload_max_bytes
    data = bytearray()
    while chunk := await file.read(CHUNK):
        data += chunk
        if len(data) > limit:
            raise ValidationFailed(
                f"The file is larger than {limit // (1024 * 1024)} MB",
                errors=[{"field": "file", "message": "file too large"}],
                status_code=413,
                code="file_too_large",
            )
    return await svc.create_upload(file.filename or "upload", file.content_type, bytes(data))


@router.get("/imports/uploads/{upload_id}", response_model=UploadRead)
async def get_upload(upload_id: uuid.UUID, _: Uploader, svc: Svc) -> UploadRead:
    return await svc.get_upload(upload_id)


@router.post("/imports/excel/preview", response_model=ImportPreview)
async def preview_import(body: ImportRequest, _: Uploader, svc: Svc) -> ImportPreview:
    """Dry run on the first rows with this mapping: parsed values and the predicted outcome of each row."""
    return await svc.preview(body)


@router.post("/imports/excel", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def start_import(
    body: ImportRequest,
    _: Uploader,
    svc: Svc,
    c: ContainerDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> JobAccepted:
    """Starts the import as a background job; follow it with GET /jobs/{id}/events."""
    job = await svc.start(body, idempotency_key, c.job_runner.notify)
    prefix = c.settings.api_prefix
    return JobAccepted(job_id=job.id, status=job.status, events_url=f"{prefix}/jobs/{job.id}/events")


@router.get("/imports/{job_id}/rows", response_model=ImportRowPage)
async def import_rows(
    job_id: uuid.UUID,
    _: Uploader,
    svc: Svc,
    outcome: ImportRowOutcome | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
) -> ImportRowPage:
    """The import report, one entry per spreadsheet row, in sheet order."""
    rows, nxt, counts = await svc.rows(job_id, outcome=outcome, limit=limit, cursor=cursor)
    return ImportRowPage(
        items=[ImportRowRead.model_validate(r) for r in rows],
        next_cursor=nxt,
        has_more=nxt is not None,
        counts=counts,
    )


@router.get("/imports/{job_id}/report.csv", response_class=Response)
async def import_report_csv(job_id: uuid.UUID, _: Uploader, svc: Svc) -> Response:
    body = await svc.report_csv(job_id)
    return Response(
        content="﻿" + body,  # BOM so Excel opens it as UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="import-{job_id}.csv"'},
    )
