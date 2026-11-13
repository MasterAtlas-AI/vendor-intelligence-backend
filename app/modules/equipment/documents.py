"""Project documents (SOW 3.13 vendor drawings, 3.14 correspondence, 3.17 / 14.6 schedules): a register beside the
project's equipment history. Each document is typed, numbered and revised, may point at an equipment line (a
vendor's GA drawing for P-101A, the package schedule) and a vendor, and keeps its file for download.

Tenant-private (FORCE'd row-level security). Files stay on the server; nothing is sent to an AI. Their text is read
on the server for search across projects (SOW H.4 / 25.4), and a datasheet's ratings can be copied onto its
equipment line (SOW 3.11 / 3.12 / 8.4).
"""

import hashlib
import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Depends, Form, Query, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, cast, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer
from sqlalchemy.sql.elements import ColumnElement

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, require
from app.api.files import attachment
from app.core.exceptions import NotFound, ValidationFailed
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.audit.service import record_audit
from app.modules.equipment.doc_text import as_value, read_ratings, read_text
from app.modules.equipment.models import EquipmentAttribute, ProjectDocument, ProjectEquipment
from app.modules.projects.models import Project
from app.modules.vendors.models import Vendor

router = APIRouter(tags=["project history"])
Reader = Annotated[Principal, Depends(require(P.EQUIPMENT_READ))]
Manager = Annotated[Principal, Depends(require(P.EQUIPMENT_MANAGE))]

DocType = Literal[
    "drawing", "datasheet", "specification", "correspondence", "schedule", "report", "certificate", "other"
]
CHUNK = 1024 * 1024


class ProjectDocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    equipment_id: uuid.UUID | None
    equipment_label: str = ""  # "P-101A · Seawater lift pump"
    vendor_id: uuid.UUID | None
    vendor_name: str = ""
    doc_type: DocType
    doc_number: str
    title: str
    revision: str
    doc_date: date | None
    department: str | None
    notes: str
    filename: str
    content_type: str
    size_bytes: int
    text_chars: int  # characters of text read from the file; -1 not read yet, 0 none (a drawing)
    created_at: datetime


class ProjectDocumentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_type: DocType | None = None
    doc_number: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    revision: str | None = Field(default=None, max_length=20)
    doc_date: date | None = None
    equipment_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=5000)


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ValidationFailed("Project documents belong to an organization; open the app as one to use them")
    return principal.tenant_id


async def _labels(session: SessionDep, docs: list[ProjectDocument]) -> list[ProjectDocumentRead]:
    eq_ids = {d.equipment_id for d in docs if d.equipment_id}
    v_ids = {d.vendor_id for d in docs if d.vendor_id}
    lines = {
        e.id: " · ".join(x for x in (e.tag, e.name) if x)
        for e in (
            await session.scalars(select(ProjectEquipment).where(ProjectEquipment.id.in_(eq_ids)))
            if eq_ids
            else []
        )
    }
    names = (
        dict(
            (await session.execute(select(Vendor.id, Vendor.display_name).where(Vendor.id.in_(v_ids)))).all()
        )
        if v_ids
        else {}
    )
    return [
        ProjectDocumentRead.model_validate(d).model_copy(
            update={
                "equipment_label": lines.get(d.equipment_id, "") if d.equipment_id else "",
                "vendor_name": names.get(d.vendor_id, "") if d.vendor_id else "",
            }
        )
        for d in docs
    ]


async def _check_links(
    session: SessionDep, project_id: uuid.UUID, equipment_id: uuid.UUID | None, vendor_id: uuid.UUID | None
) -> None:
    if equipment_id:
        line = await session.get(ProjectEquipment, equipment_id)
        if line is None or line.project_id != project_id:
            raise ValidationFailed("That equipment line is not on this project")
    if vendor_id and await session.get(Vendor, vendor_id) is None:
        raise NotFound("Vendor not found")


@router.get("/projects/{project_id}/documents", response_model=list[ProjectDocumentRead])
async def list_documents(
    project_id: uuid.UUID,
    _: Reader,
    session: SessionDep,
    doc_type: DocType | None = None,
    equipment_id: uuid.UUID | None = None,
    q: Annotated[str, Query(max_length=120)] = "",
) -> list[ProjectDocumentRead]:
    """The project's documents, newest first; filter by type, by equipment line, or by number / title text."""
    if await session.get(Project, project_id) is None:
        raise NotFound("Project not found")
    stmt = select(ProjectDocument).where(ProjectDocument.project_id == project_id)
    if doc_type:
        stmt = stmt.where(ProjectDocument.doc_type == doc_type)
    if equipment_id:
        stmt = stmt.where(ProjectDocument.equipment_id == equipment_id)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                ProjectDocument.doc_number.ilike(like),
                ProjectDocument.title.ilike(like),
                ProjectDocument.filename.ilike(like),
            )
        )
    docs = list(await session.scalars(stmt.order_by(ProjectDocument.created_at.desc()).limit(500)))
    return await _labels(session, docs)


@router.post(
    "/projects/{project_id}/documents",
    response_model=ProjectDocumentRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_document(
    project_id: uuid.UUID,
    principal: Manager,
    session: SessionDep,
    c: ContainerDep,
    file: UploadFile,
    doc_type: Annotated[DocType, Form()],
    title: Annotated[str, Form(max_length=300)] = "",
    doc_number: Annotated[str, Form(max_length=120)] = "",
    revision: Annotated[str, Form(max_length=20)] = "",
    doc_date: Annotated[date | None, Form()] = None,
    equipment_id: Annotated[uuid.UUID | None, Form()] = None,
    vendor_id: Annotated[uuid.UUID | None, Form()] = None,
    notes: Annotated[str, Form(max_length=5000)] = "",
) -> ProjectDocumentRead:
    """Adds a document with its file (any type: PDF, DWG, Word, e-mail...). Title defaults to the file name."""
    tenant_id = _tenant(principal)
    if await session.get(Project, project_id) is None:
        raise NotFound("Project not found")
    await _check_links(session, project_id, equipment_id, vendor_id)
    limit = c.settings.upload_max_bytes
    data = bytearray()
    while chunk := await file.read(CHUNK):
        data += chunk
        if len(data) > limit:
            raise ValidationFailed(
                f"The file is larger than {limit // (1024 * 1024)} MB", status_code=413, code="file_too_large"
            )
    if not data:
        raise ValidationFailed("The file is empty")
    filename = (file.filename or "document").replace("\\", "/").rsplit("/", 1)[-1][:255]
    doc = ProjectDocument(
        tenant_id=tenant_id,
        project_id=project_id,
        equipment_id=equipment_id,
        vendor_id=vendor_id,
        doc_type=doc_type,
        doc_number=doc_number.strip(),
        title=(title.strip() or filename)[:300],
        revision=revision.strip(),
        doc_date=doc_date,
        notes=notes,
        filename=filename,
        content_type=(file.content_type or "application/octet-stream")[:120],
        size_bytes=len(data),
        data=bytes(data),
    )
    await _store_text(doc, bytes(data))
    session.add(doc)
    await session.flush()
    record_audit(
        session,
        "project_document.added",
        principal=principal,
        entity_type="project_document",
        entity_id=doc.id,
        changes={
            "project_id": str(project_id),
            "doc_type": doc_type,
            "filename": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    await session.commit()
    return (await _labels(session, [doc]))[0]


async def _store_text(doc: ProjectDocument, data: bytes) -> None:
    text = (await anyio.to_thread.run_sync(read_text, data, doc.filename)).text
    doc.text_content, doc.text_chars = text, len(text)


async def _read_unread(session: AsyncSession, limit: int = 5) -> None:
    """Reads the text of documents added before text was kept (a few per call, so a search stays quick)."""
    unread = list(
        await session.scalars(
            select(ProjectDocument)
            .where(ProjectDocument.text_chars < 0)
            .options(undefer(ProjectDocument.data))
            .limit(limit)
        )
    )
    for doc in unread:
        await _store_text(doc, doc.data or b"")
    if unread:
        await session.commit()


class DocumentHit(ProjectDocumentRead):
    project_name: str
    project_code: str | None
    snippet: str  # words around the match, matched words wrapped in [[ ]]; empty when only the title matched


class DocumentSearch(BaseModel):
    items: list[DocumentHit]
    total: int


_VECTOR = (
    "to_tsvector('english', coalesce(project_documents.title, '') || ' ' || "
    "coalesce(project_documents.doc_number, '') || ' ' || coalesce(project_documents.text_content, ''))"
)
_HEADLINE = 'MaxWords=35, MinWords=15, StartSel=[[, StopSel=]], MaxFragments=2, FragmentDelimiter=" … "'


def _query(text: str, any_word: bool) -> ColumnElement[Any]:
    """All the words (a search box), or any of them (the assistant's plain-language request), stemmed."""
    if not any_word:
        return func.websearch_to_tsquery("english", text)
    words = list(dict.fromkeys(w.lower() for w in re.findall(r"[A-Za-z0-9]{3,}", text)))[:16]
    return func.to_tsquery("english", " | ".join(words) or "''")


async def find_documents(
    session: AsyncSession,
    q: str,
    *,
    any_word: bool = False,
    doc_type: str | None = None,
    project_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list["DocumentHit"], int]:
    """The organization's documents by their words, number, title or file name; best matches first, each with the
    passage that matched. Row-level security keeps it to the caller's organization."""
    await _read_unread(session)
    text = q.strip()
    tsq = _query(text, any_word)
    vector: ColumnElement[Any] = literal_column(_VECTOR)
    like = f"%{text}%"
    cond = or_(
        vector.op("@@")(tsq),
        ProjectDocument.doc_number.ilike(like),
        ProjectDocument.title.ilike(like),
        ProjectDocument.filename.ilike(like),
    )
    base = select(ProjectDocument).where(cond)
    if doc_type:
        base = base.where(ProjectDocument.doc_type == doc_type)
    if project_id:
        base = base.where(ProjectDocument.project_id == project_id)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rank = func.ts_rank(vector, tsq) + cast(ProjectDocument.title.ilike(like), Integer)
    snippet = func.ts_headline(
        "english", func.left(func.coalesce(ProjectDocument.text_content, ""), 100_000), tsq, _HEADLINE
    )
    rows = (
        await session.execute(
            base.add_columns(snippet.label("snippet"))
            .order_by(rank.desc(), ProjectDocument.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    docs = [r[0] for r in rows]
    projects = (
        {
            p.id: p
            for p in await session.scalars(
                select(Project).where(Project.id.in_({d.project_id for d in docs}))
            )
        }
        if docs
        else {}
    )
    items = []
    for read, (doc, found) in zip(await _labels(session, docs), rows, strict=True):
        project = projects.get(doc.project_id)
        items.append(
            DocumentHit(
                **read.model_dump(),
                project_name=project.name if project else "",
                project_code=project.code if project else None,
                snippet=(found or "")[:600] if "[[" in (found or "") else "",
            )
        )
    return items, total


@router.get("/project-documents/search", response_model=DocumentSearch)
async def search_documents(
    _: Reader,
    session: SessionDep,
    q: Annotated[str, Query(min_length=2, max_length=200)],
    doc_type: DocType | None = None,
    project_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentSearch:
    """Every project's documents by their words (reports, letters, datasheets...), number, title or file name."""
    items, total = await find_documents(
        session, q, doc_type=doc_type, project_id=project_id, limit=limit, offset=offset
    )
    return DocumentSearch(items=items, total=total)


class RatingFound(BaseModel):
    key: str
    label: str
    unit: str
    found: float | list[float]
    current: float | list[float] | str | None
    source: str  # the datasheet line it was read from


class DocumentRatings(BaseModel):
    equipment_id: uuid.UUID | None
    equipment_label: str
    equipment_version: int | None
    ratings: list[RatingFound]


@router.get("/project-documents/{document_id}/ratings", response_model=DocumentRatings)
async def document_ratings(document_id: uuid.UUID, _: Reader, session: SessionDep) -> DocumentRatings:
    """The ratings a datasheet or specification gives ("Rated flow: 120 m3/h"), beside its equipment line's
    current values; copying them onto the line is an ordinary equipment edit."""
    doc = await session.scalar(
        select(ProjectDocument)
        .where(ProjectDocument.id == document_id)
        .options(undefer(ProjectDocument.data))
    )
    if doc is None:
        raise NotFound("Document not found")
    parsed = await anyio.to_thread.run_sync(read_text, doc.data or b"", doc.filename)
    found, source = read_ratings(parsed)
    line = await session.get(ProjectEquipment, doc.equipment_id) if doc.equipment_id else None
    defs = {a.key: a for a in await session.scalars(select(EquipmentAttribute))}
    current = (line.attributes or {}) if line else {}
    ratings = [
        RatingFound(
            key=key,
            label=defs[key].label if key in defs else key.replace("_", " ").capitalize(),
            unit=defs[key].unit if key in defs else "",
            found=as_value(span),
            current=current.get(key),
            source=source[key],
        )
        for key, span in found.items()
    ]
    return DocumentRatings(
        equipment_id=line.id if line else None,
        equipment_label=" · ".join(x for x in (line.tag, line.name) if x) if line else "",
        equipment_version=line.version if line else None,
        ratings=ratings,
    )


@router.patch("/project-documents/{document_id}", response_model=ProjectDocumentRead)
async def update_document(
    document_id: uuid.UUID, body: ProjectDocumentUpdate, principal: Manager, session: SessionDep
) -> ProjectDocumentRead:
    doc = await session.get(ProjectDocument, document_id)
    if doc is None:
        raise NotFound("Document not found")
    changes = body.model_dump(exclude_unset=True)
    await _check_links(session, doc.project_id, changes.get("equipment_id"), changes.get("vendor_id"))
    for key, value in changes.items():
        setattr(
            doc,
            key,
            value
            if value is not None or key in {"equipment_id", "vendor_id", "doc_date"}
            else getattr(doc, key),
        )
    record_audit(
        session,
        "project_document.updated",
        principal=principal,
        entity_type="project_document",
        entity_id=doc.id,
        changes={k: str(v)[:200] for k, v in changes.items()},
    )
    await session.commit()
    return (await _labels(session, [doc]))[0]


@router.delete("/project-documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: uuid.UUID, principal: Manager, session: SessionDep) -> None:
    doc = await session.get(ProjectDocument, document_id)
    if doc is None:
        raise NotFound("Document not found")
    record_audit(
        session,
        "project_document.deleted",
        principal=principal,
        entity_type="project_document",
        entity_id=doc.id,
        changes={"filename": doc.filename, "doc_number": doc.doc_number, "project_id": str(doc.project_id)},
    )
    await session.delete(doc)
    await session.commit()


@router.get("/project-documents/{document_id}/file", response_class=Response)
async def download_document(
    document_id: uuid.UUID, _: Reader, session: SessionDep, __: PrincipalDep
) -> Response:
    doc = await session.scalar(
        select(ProjectDocument)
        .where(ProjectDocument.id == document_id)
        .options(undefer(ProjectDocument.data))
    )
    if doc is None or doc.data is None:
        raise NotFound("Document not found")
    return Response(
        doc.data,
        media_type=doc.content_type or "application/octet-stream",
        headers=attachment(doc.filename, "document"),
    )
