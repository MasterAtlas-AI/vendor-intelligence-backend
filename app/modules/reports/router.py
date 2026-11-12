import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, SessionDep, require
from app.core.exceptions import Conflict
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.reports.service import ReportService

router = APIRouter(tags=["reports"])

ReportType = Literal["vendor_comparison", "market_scan", "avl", "rfq_summary"]


class ReportRequest(BaseModel):
    """parameters by type: vendor_comparison {vendor_ids} or {project_id}; market_scan {equipment_category,
    countries?}; avl {avl_status?}; rfq_summary {rfq_id}."""

    model_config = ConfigDict(extra="forbid")
    report_type: ReportType
    format: Literal["pdf", "xlsx"] = "pdf"
    parameters: dict[str, Any] = Field(default_factory=dict)


class ReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    report_type: ReportType
    format: Literal["pdf", "xlsx"]
    title: str
    parameters: dict[str, Any]
    status: Literal["queued", "ready", "failed"]
    error: str | None
    filename: str | None
    size_bytes: int | None
    job_id: uuid.UUID | None
    created_at: datetime


def _svc(
    session: SessionDep, principal: Annotated[Principal, Depends(require(P.REPORT_READ))], c: ContainerDep
) -> ReportService:
    return ReportService(session, principal, c.job_runner.notify)


Svc = Annotated[ReportService, Depends(_svc)]


@router.post("/reports", response_model=ReportRead, status_code=status.HTTP_202_ACCEPTED)
async def request_report(body: ReportRequest, svc: Svc) -> ReportRead:
    """Generate a report in the background; follow it with GET /reports/{id} (or its job), then download it."""
    return ReportRead.model_validate(await svc.request(body.report_type, body.format, body.parameters))


@router.get("/reports", response_model=list[ReportRead])
async def list_reports(svc: Svc, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[ReportRead]:
    return [ReportRead.model_validate(r) for r in await svc.list_reports(limit)]


@router.get("/reports/{report_id}", response_model=ReportRead)
async def get_report(report_id: uuid.UUID, svc: Svc) -> ReportRead:
    return ReportRead.model_validate(await svc.get(report_id))


@router.get("/reports/{report_id}/download", response_class=Response)
async def download_report(
    report_id: uuid.UUID, svc: Svc, _: Annotated[Principal, Depends(require(P.DATA_EXPORT))]
) -> Response:
    """Downloading data out of the system needs data:export (SOW 21); viewing a report's status doesn't."""
    report = await svc.get(report_id, with_data=True)
    if report.status != "ready" or report.data is None:
        raise Conflict("The report isn't ready yet" if report.status == "queued" else "The report failed")
    return Response(
        content=report.data,
        media_type=report.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{report.filename}"'},
    )
