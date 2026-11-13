import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, UploadFile, status

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, require
from app.api.files import attachment
from app.core.exceptions import ValidationFailed
from app.domain.enums import JobType
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.equipment.schemas import (
    AttributeRead,
    AttributeWrite,
    CategoryNode,
    EquipmentPage,
    EquipmentSearch,
    EquipmentUpdate,
    EquipmentWrite,
    MelDecision,
    MelDecisionResult,
    MelParseRequest,
    MelRowRead,
    MelRows,
    MelRowUpdate,
    MelUploadRead,
    MelUploadSummary,
    NoteKind,
    NoteRead,
    NoteWrite,
    ProcurementRead,
    ProcurementWrite,
    ProjectEquipmentRead,
    SavedSearchRead,
    SavedSearchWrite,
    SimilarRequest,
    SimilarResult,
    VendorHistory,
)
from app.modules.equipment.service import EquipmentService
from app.modules.jobs.schemas import JobAccepted
from app.modules.jobs.service import JobService

router = APIRouter(tags=["project history"])

CHUNK = 1024 * 1024


def _svc(session: SessionDep, principal: PrincipalDep) -> EquipmentService:
    return EquipmentService(session, principal)


Svc = Annotated[EquipmentService, Depends(_svc)]
Reader = Annotated[Principal, Depends(require(P.EQUIPMENT_READ))]
Manager = Annotated[Principal, Depends(require(P.EQUIPMENT_MANAGE))]


# ---------------------------------------------------------------- equipment lines


@router.post("/equipment/search", response_model=EquipmentPage)
async def search_equipment(body: EquipmentSearch, _: Reader, svc: Svc) -> EquipmentPage:
    """The organization's equipment history (from its projects' MELs), with filters: project, category, vendor,
    country, facility type, offshore/onshore, year, standard, material, verification and attribute ranges."""
    return await svc.search(body)


@router.post("/equipment/similar", response_model=SimilarResult)
async def similar_equipment(body: SimilarRequest, _: Reader, svc: Svc) -> SimilarResult:
    """Near-fit search: historical lines and catalogue products closest to the requested category and ratings,
    with each difference spelled out; optional interpolated estimates, labelled as not verified (SOW 12, 13)."""
    return await svc.similar(body)


@router.post("/equipment", response_model=ProjectEquipmentRead, status_code=status.HTTP_201_CREATED)
async def create_equipment(body: EquipmentWrite, _: Manager, svc: Svc) -> ProjectEquipmentRead:
    return await svc.create(body)


@router.get("/equipment/{equipment_id}", response_model=ProjectEquipmentRead)
async def get_equipment(equipment_id: uuid.UUID, _: Reader, svc: Svc) -> ProjectEquipmentRead:
    return await svc.get(equipment_id)


@router.get("/equipment/{equipment_id}/source", response_class=Response)
async def equipment_source(equipment_id: uuid.UUID, _: Reader, svc: Svc) -> Response:
    """The original file this record came from (the uploaded MEL), for traceability (SOW 17)."""
    upload = await svc.source_file(equipment_id)
    return Response(
        content=upload.data,
        media_type=upload.content_type or "application/octet-stream",
        headers=attachment(upload.filename),
    )


@router.patch("/equipment/{equipment_id}", response_model=ProjectEquipmentRead)
async def update_equipment(
    equipment_id: uuid.UUID, body: EquipmentUpdate, _: Manager, svc: Svc
) -> ProjectEquipmentRead:
    return await svc.update(equipment_id, body)


@router.delete("/equipment/{equipment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_equipment(equipment_id: uuid.UUID, _: Manager, svc: Svc) -> None:
    """Marks the line archived / superseded; it stays for traceability but leaves searches."""
    await svc.archive(equipment_id)


# ---------------------------------------------------------------- MEL import


@router.post("/projects/{project_id}/mel", response_model=MelUploadRead, status_code=status.HTTP_201_CREATED)
async def upload_mel(
    project_id: uuid.UUID, file: UploadFile, _: Manager, svc: Svc, c: ContainerDep
) -> MelUploadRead:
    """Stores a project's MEL (.xlsx, .csv, or a Word table) and returns its sheets with a suggested column
    mapping. Nothing is recorded yet: parse it, review the rows, then approve them."""
    limit = c.settings.upload_max_bytes
    data = bytearray()
    while chunk := await file.read(CHUNK):
        data += chunk
        if len(data) > limit:
            raise ValidationFailed(
                f"The file is larger than {limit // (1024 * 1024)} MB", status_code=413, code="file_too_large"
            )
    return await svc.upload_mel(
        project_id, file.filename or "mel", file.content_type, bytes(data), c.settings.import_max_rows
    )


@router.get("/projects/{project_id}/mel", response_model=list[MelUploadSummary])
async def project_mels(project_id: uuid.UUID, _: Reader, svc: Svc) -> list[MelUploadSummary]:
    """The MELs uploaded to a project, newest first, with each one's latest parse and its rows by status."""
    return await svc.mel_uploads(project_id)


@router.post("/mel/{upload_id}/parse", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def parse_mel(
    upload_id: uuid.UUID,
    body: MelParseRequest,
    principal: Manager,
    svc: Svc,
    session: SessionDep,
    c: ContainerDep,
) -> JobAccepted:
    """Reads every row in the background: category, vendor and manufacturer matches, ratings into attributes.
    The rows then wait for review (GET /mel/{upload_id}/rows)."""
    upload = await svc.mel_upload(upload_id)
    if upload.meta.get("project_id") != str(body.project_id):
        raise ValidationFailed("This MEL was uploaded for another project")
    job = await JobService(session, principal, c.job_runner.notify).create(
        JobType.MEL_IMPORT, {"upload_id": str(upload_id), **body.model_dump(mode="json")}, None
    )
    return JobAccepted(
        job_id=job.id, status=job.status, events_url=f"{c.settings.api_prefix}/jobs/{job.id}/events"
    )


@router.get("/mel/{upload_id}/rows", response_model=MelRows)
async def mel_rows(
    upload_id: uuid.UUID,
    _: Reader,
    svc: Svc,
    status_: Annotated[str | None, Query(alias="status", pattern="^(pending|approved|rejected)$")] = None,
) -> MelRows:
    return await svc.mel_rows(upload_id, status_)


@router.patch("/mel/rows/{row_id}", response_model=MelRowRead)
async def update_mel_row(row_id: uuid.UUID, body: MelRowUpdate, _: Manager, svc: Svc) -> MelRowRead:
    """Fixes a row before approval (category, vendor, description, ratings)."""
    return await svc.update_mel_row(row_id, body)


@router.post("/mel/rows/decide", response_model=MelDecisionResult)
async def decide_mel_rows(body: MelDecision, _: Manager, svc: Svc) -> MelDecisionResult:
    """Approved rows become the project's equipment history (and PO numbers procurement records); matched vendors
    join the organization's vendor list."""
    return await svc.decide_mel(body)


# ---------------------------------------------------------------- procurement and notes


@router.get("/procurement-records", response_model=list[ProcurementRead])
async def list_procurement(
    _: Reader,
    svc: Svc,
    vendor_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    equipment_id: uuid.UUID | None = None,
) -> list[ProcurementRead]:
    """Purchase orders and deliveries. Amounts appear only with commercial:read."""
    return await svc.procurement(vendor_id, project_id, equipment_id)


@router.post("/procurement-records", response_model=ProcurementRead, status_code=status.HTTP_201_CREATED)
async def create_procurement(body: ProcurementWrite, _: Manager, svc: Svc) -> ProcurementRead:
    return await svc.create_procurement(body)


@router.put("/procurement-records/{record_id}", response_model=ProcurementRead)
async def update_procurement(
    record_id: uuid.UUID, body: ProcurementWrite, _: Manager, svc: Svc
) -> ProcurementRead:
    return await svc.update_procurement(record_id, body)


@router.delete("/procurement-records/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_procurement(record_id: uuid.UUID, _: Manager, svc: Svc) -> None:
    await svc.delete_procurement(record_id)


@router.get("/knowledge-notes", response_model=list[NoteRead])
async def list_notes(
    _: Reader,
    svc: Svc,
    vendor_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    equipment_id: uuid.UUID | None = None,
    kind: NoteKind | None = None,
) -> list[NoteRead]:
    """Lessons learned, operating feedback, maintenance and spares notes."""
    return await svc.notes(vendor_id, project_id, equipment_id, kind)


@router.post("/knowledge-notes", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
async def create_note(body: NoteWrite, _: Reader, svc: Svc) -> NoteRead:
    return await svc.create_note(body)


@router.delete("/knowledge-notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: uuid.UUID, _: Reader, svc: Svc) -> None:
    await svc.delete_note(note_id)


@router.get("/vendors/{vendor_id}/history", response_model=VendorHistory)
async def vendor_history(vendor_id: uuid.UUID, _: Reader, svc: Svc) -> VendorHistory:
    """The organization's own experience with a vendor: projects, equipment, lead times, on-time rate, rating."""
    return await svc.vendor_history(vendor_id)


# ---------------------------------------------------------------- attributes, saved searches, categories


@router.get("/equipment-attributes", response_model=list[AttributeRead])
async def list_attributes(_: Reader, svc: Svc) -> list[AttributeRead]:
    return await svc.attributes()


@router.post("/equipment-attributes", response_model=AttributeRead, status_code=status.HTTP_201_CREATED)
async def create_attribute(body: AttributeWrite, _: Manager, svc: Svc) -> AttributeRead:
    return await svc.create_attribute(body)


@router.delete("/equipment-attributes/{attribute_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attribute(attribute_id: uuid.UUID, _: Manager, svc: Svc) -> None:
    await svc.delete_attribute(attribute_id)


@router.get("/saved-searches", response_model=list[SavedSearchRead])
async def list_saved_searches(
    _: Annotated[Principal, Depends(require(P.SEARCH_RUN))],
    svc: Svc,
    kind: Annotated[str | None, Query(pattern="^(vendors|equipment)$")] = None,
) -> list[SavedSearchRead]:
    return [SavedSearchRead.model_validate(s) for s in await svc.saved_searches(kind)]


@router.post("/saved-searches", response_model=SavedSearchRead, status_code=status.HTTP_201_CREATED)
async def save_search(
    body: SavedSearchWrite, _: Annotated[Principal, Depends(require(P.SEARCH_RUN))], svc: Svc
) -> SavedSearchRead:
    return SavedSearchRead.model_validate(await svc.save_search(body))


@router.delete("/saved-searches/{search_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_search(
    search_id: uuid.UUID, _: Annotated[Principal, Depends(require(P.SEARCH_RUN))], svc: Svc
) -> None:
    await svc.delete_saved_search(search_id)


@router.get("/categories/browse", response_model=list[CategoryNode])
async def browse_categories(
    _: Annotated[Principal, Depends(require(P.VENDOR_READ))], svc: Svc
) -> list[CategoryNode]:
    """The equipment taxonomy as a tree, with knowledge-base vendors and the organization's equipment per node."""
    return await svc.category_tree()
