"""Uploads, mapping preview, starting an import job, and reading its report."""

import csv
import hashlib
import io
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import anyio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.config import Settings
from app.core.exceptions import NotFound, ValidationFailed
from app.domain.enums import ImportRowOutcome, JobType, ProposalOrigin, UploadKind, VendorStatus
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.audit.service import record_audit
from app.modules.imports.importer import ImportOptions, Taxonomy, VendorListImporter, parse_row
from app.modules.imports.models import ImportRow, Upload
from app.modules.imports.pdf_import import (
    DOCUMENT_EVIDENCE,
    candidates,
    pdf_info,
    read_and_extract,
    vendor_row,
)
from app.modules.imports.schemas import (
    ImportPreview,
    ImportRequest,
    PdfImportRequest,
    PdfInfo,
    PdfPreview,
    PdfVendorMatch,
    PreviewRow,
    SheetInfo,
    UploadRead,
)
from app.modules.imports.spreadsheet import (
    HEADER_SCAN_ROWS,
    SPREADSHEET_EXTENSIONS,
    Sheet,
    Workbook,
    data_rows,
    detect_header_row,
    extension,
    headers,
    read_spreadsheet,
    suggest_mapping,
)
from app.modules.jobs.models import Job
from app.modules.jobs.service import JobService
from app.modules.tenants.models import Tenant
from app.modules.vendors.models import Vendor

SAMPLE_ROWS = 5
# Jobs whose per-row results live in import_rows (imports and AI discovery runs).
REPORT_JOB_TYPES = {JobType.EXCEL_IMPORT, JobType.PDF_IMPORT, JobType.VENDOR_DISCOVERY, JobType.EQUIPMENT_DISCOVERY,
                    JobType.SERVICE_DISCOVERY, JobType.ENRICHMENT}  # fmt: skip
PREVIEW_ROWS = 20


def options_from(request: ImportRequest | PdfImportRequest) -> ImportOptions:
    return ImportOptions(
        default_avl_status=request.options.default_avl_status, tags=list(request.options.tags)
    )


def resolve_sheet(book: Workbook, request: ImportRequest) -> tuple[Sheet, int, list[str]]:
    sheet = book.sheet(request.sheet)
    header_row = request.header_row or detect_header_row(sheet)
    names = headers(sheet, header_row)
    too_far = {f: i for f, i in request.mapping.items() if i >= len(names)}
    if too_far:
        raise ValidationFailed(
            "The mapping points at columns the sheet doesn't have",
            errors=[
                {
                    "field": f"mapping.{f}",
                    "message": f"column {i} is beyond the last column ({len(names) - 1})",
                }
                for f, i in too_far.items()
            ],
        )
    return sheet, header_row, names


async def read_upload(upload: Upload, max_rows: int) -> Workbook:
    data, filename = upload.data, upload.filename
    return await anyio.to_thread.run_sync(lambda: read_spreadsheet(data, filename, max_rows=max_rows))


async def may_auto_approve(session: AsyncSession, principal: Principal) -> bool:
    """Trusted tenants' and curators' imports go straight into the knowledge base (design A1)."""
    if principal.has(P.KNOWLEDGE_APPROVE):
        return True
    if principal.tenant_id is None:
        return False
    return bool(await session.scalar(select(Tenant.trusted_source).where(Tenant.id == principal.tenant_id)))


class ImportService:
    def __init__(self, session: AsyncSession, principal: Principal, settings: Settings) -> None:
        self.s = session
        self.principal = principal
        self.settings = settings

    @property
    def _read_limit(self) -> int:
        return self.settings.import_max_rows + HEADER_SCAN_ROWS + 1

    # ------------------------------------------------------------------ uploads

    def _sheet_info(self, sheet: Sheet) -> SheetInfo:
        header_row = detect_header_row(sheet)
        names = headers(sheet, header_row) if sheet.rows else []
        rows, truncated = data_rows(sheet, header_row, self.settings.import_max_rows)
        return SheetInfo(
            name=sheet.name,
            header_row=header_row,
            headers=names,
            data_rows=len(rows),
            truncated=truncated,
            sample=[values for _, values in rows[:SAMPLE_ROWS]],
            suggested_mapping=suggest_mapping(names),
        )

    async def create_upload(self, filename: str, content_type: str | None, data: bytes) -> UploadRead:
        filename = (filename or "upload").replace("\\", "/").rsplit("/", 1)[-1][:255]
        ext = extension(filename)
        if ext not in SPREADSHEET_EXTENSIONS and ext not in (".pdf", ".docx"):
            raise ValidationFailed(
                "Upload an Excel workbook (.xlsx), a CSV file, a PDF or a Word document (.docx)",
                errors=[{"field": "file", "message": f"unsupported file type: {filename}"}],
            )
        if not data:
            raise ValidationFailed("The file is empty", errors=[{"field": "file", "message": "empty file"}])
        if ext in (".pdf", ".docx"):
            kind = UploadKind.PDF  # a vendor document: PDF or Word, read the same way
            pdf, ex = await read_and_extract(
                data, await Taxonomy.load(self.s), self.settings.pdf_max_pages, filename
            )
            if not pdf.page_count:
                raise ValidationFailed(
                    "The PDF has no pages", errors=[{"field": "file", "message": "no pages"}]
                )
            meta: dict[str, Any] = {"pdf": pdf_info(pdf, ex).model_dump()}
        else:
            kind = UploadKind.SPREADSHEET
            limit = self._read_limit
            book = await anyio.to_thread.run_sync(lambda: read_spreadsheet(data, filename, max_rows=limit))
            sheets = [self._sheet_info(sheet) for sheet in book.sheets]
            if not any(s.data_rows for s in sheets):
                raise ValidationFailed(
                    "The file has no data rows", errors=[{"field": "file", "message": "no rows"}]
                )
            meta = {"sheets": [s.model_dump() for s in sheets]}

        upload = Upload(
            tenant_id=self.principal.tenant_id,
            kind=kind,
            filename=filename,
            content_type=(content_type or "application/octet-stream")[:120],
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            meta=meta,
            data=data,
        )
        self.s.add(upload)
        await self.s.flush()
        record_audit(
            self.s,
            "upload.created",
            principal=self.principal,
            entity_type="upload",
            entity_id=upload.id,
            changes={"filename": filename, "size_bytes": len(data)},
        )
        await self.s.commit()
        return self._read(upload)

    @staticmethod
    def _read(upload: Upload) -> UploadRead:
        return UploadRead(
            id=upload.id,
            kind=upload.kind,
            filename=upload.filename,
            content_type=upload.content_type,
            size_bytes=upload.size_bytes,
            sha256=upload.sha256,
            created_at=upload.created_at,
            sheets=[SheetInfo.model_validate(s) for s in upload.meta.get("sheets", [])],
            pdf=PdfInfo.model_validate(upload.meta["pdf"]) if "pdf" in upload.meta else None,
        )

    async def _upload(
        self, upload_id: uuid.UUID, *, with_data: bool = False, kind: UploadKind | None = None
    ) -> Upload:
        stmt = select(Upload).where(Upload.id == upload_id)
        if with_data:
            stmt = stmt.options(undefer(Upload.data))
        upload = await self.s.scalar(stmt)  # RLS keeps other tenants' uploads invisible
        if upload is None:
            raise NotFound("Upload not found")
        if kind is not None and upload.kind != kind:
            expected = "a spreadsheet (.xlsx/.csv)" if kind == UploadKind.SPREADSHEET else "a PDF"
            raise ValidationFailed(
                f"This import needs {expected}",
                errors=[{"field": "upload_id", "message": f"the upload is a {upload.kind.value}"}],
            )
        return upload

    async def get_upload(self, upload_id: uuid.UUID) -> UploadRead:
        return self._read(await self._upload(upload_id))

    # ------------------------------------------------------------------ preview & start

    async def preview(self, request: ImportRequest) -> ImportPreview:
        """Dry run on the first rows: shows how cells map and what the import would do, without writing."""
        upload = await self._upload(request.upload_id, with_data=True, kind=UploadKind.SPREADSHEET)
        book = await read_upload(upload, self._read_limit)
        sheet, header_row, names = resolve_sheet(book, request)
        all_rows, _ = data_rows(sheet, header_row, self.settings.import_max_rows)
        options = options_from(request)
        taxonomy = await Taxonomy.load(self.s)
        importer = VendorListImporter(
            self.s,
            taxonomy,
            tenant_id=self.principal.tenant_id,
            user_id=self.principal.user_id,
            job_id=None,
            upload_id=upload.id,
            auto_approve=await may_auto_approve(self.s, self.principal),
            options=options,
            dry_run=True,
        )
        rows: list[PreviewRow] = []
        predicted: dict[str, int] = {}
        for row_number, values in all_rows[:PREVIEW_ROWS]:
            parsed = parse_row(row_number, values, request.mapping, taxonomy, options)
            result = await importer.process(parsed)
            predicted[result.outcome.value] = predicted.get(result.outcome.value, 0) + 1
            rows.append(
                PreviewRow(
                    row_number=row_number,
                    values={f: values[i] for f, i in request.mapping.items() if i < len(values)},
                    outcome=result.outcome,
                    vendor_id=result.vendor_id,
                    messages=result.messages,
                )
            )
        await self.s.rollback()
        return ImportPreview(
            sheet=sheet.name,
            header_row=header_row,
            headers=names,
            data_rows=len(all_rows),
            rows=rows,
            predicted=predicted,
        )

    async def start(
        self, request: ImportRequest, idempotency_key: str | None, notify_runner: Callable[[], None]
    ) -> Job:
        upload = await self._upload(request.upload_id, with_data=True, kind=UploadKind.SPREADSHEET)
        book = await read_upload(upload, self._read_limit)
        _, header_row, _ = resolve_sheet(book, request)  # fail fast on a bad sheet/header/mapping
        payload: dict[str, Any] = {
            "upload_id": str(upload.id),
            "filename": upload.filename,
            "sheet": request.sheet or book.sheets[0].name,
            "header_row": header_row,
            "mapping": request.mapping,
            "options": {
                k: (v.value if hasattr(v, "value") else v) for k, v in asdict(options_from(request)).items()
            },
            "auto_approve": await may_auto_approve(self.s, self.principal),
        }
        return await JobService(self.s, self.principal, notify_runner).create(
            JobType.EXCEL_IMPORT, payload, idempotency_key
        )

    # ------------------------------------------------------------------ PDF

    async def preview_pdf(self, request: PdfImportRequest) -> PdfPreview:
        """The facts found in the document, with page and snippet; plus, when the vendor is named, what the
        import would do with it. Nothing is written."""
        upload = await self._upload(request.upload_id, with_data=True, kind=UploadKind.PDF)
        taxonomy = await Taxonomy.load(self.s)
        pdf, ex = await read_and_extract(upload.data, taxonomy, self.settings.pdf_max_pages, upload.filename)
        match: PdfVendorMatch | None = None
        if request.vendor is not None:
            options = options_from(request)
            row = await vendor_row(self.s, taxonomy, request.vendor, pdf, ex, request.include, options)
            auto = await may_auto_approve(self.s, self.principal)
            importer = self._pdf_importer(taxonomy, upload.id, None, options, dry_run=True, auto_approve=auto)
            result = await importer.process(row)
            match = PdfVendorMatch(
                outcome=result.outcome, vendor_id=result.vendor_id, messages=result.messages
            )
        await self.s.rollback()
        return PdfPreview(**pdf_info(pdf, ex).model_dump(), candidates=candidates(ex, taxonomy), vendor=match)

    def _pdf_importer(
        self,
        taxonomy: Taxonomy,
        upload_id: uuid.UUID,
        job_id: uuid.UUID | None,
        options: ImportOptions,
        *,
        dry_run: bool,
        auto_approve: bool = False,
    ) -> VendorListImporter:
        return VendorListImporter(
            self.s,
            taxonomy,
            tenant_id=self.principal.tenant_id,
            user_id=self.principal.user_id,
            job_id=job_id,
            upload_id=upload_id,
            auto_approve=auto_approve,
            options=options,
            dry_run=dry_run,
            origin=ProposalOrigin.PDF,
            evidence=DOCUMENT_EVIDENCE,
        )

    async def start_pdf(
        self, request: PdfImportRequest, idempotency_key: str | None, notify_runner: Callable[[], None]
    ) -> Job:
        if request.vendor is None:
            raise ValidationFailed(
                "Say whose document this is",
                errors=[{"field": "vendor", "message": "give vendor_id or the vendor's name"}],
            )
        upload = await self._upload(request.upload_id, kind=UploadKind.PDF)
        if request.vendor.vendor_id:
            vendor = await self.s.get(Vendor, request.vendor.vendor_id)
            if vendor is None or vendor.status == VendorStatus.RETIRED:
                raise NotFound("Vendor not found")
        payload: dict[str, Any] = {
            "upload_id": str(upload.id),
            "filename": upload.filename,
            "vendor": request.vendor.model_dump(mode="json"),
            "include": request.include,
            "options": request.options.model_dump(mode="json"),
            "auto_approve": await may_auto_approve(self.s, self.principal),
        }
        return await JobService(self.s, self.principal, notify_runner).create(
            JobType.PDF_IMPORT, payload, idempotency_key
        )

    # ------------------------------------------------------------------ report

    async def _import_job(self, job_id: uuid.UUID) -> Job:
        job = await JobService(self.s, self.principal, lambda: None).get(job_id)
        if job.type not in REPORT_JOB_TYPES:
            raise NotFound("Import not found")
        return job

    async def counts(self, job_id: uuid.UUID) -> dict[str, int]:
        rows = await self.s.execute(
            select(ImportRow.outcome, func.count())
            .where(ImportRow.job_id == job_id)
            .group_by(ImportRow.outcome)
        )
        return {outcome.value: n for outcome, n in rows.all()}

    async def rows(
        self, job_id: uuid.UUID, *, outcome: ImportRowOutcome | None, limit: int, cursor: str | None
    ) -> tuple[list[ImportRow], str | None, dict[str, int]]:
        await self._import_job(job_id)
        stmt = select(ImportRow).where(ImportRow.job_id == job_id)
        if outcome:
            stmt = stmt.where(ImportRow.outcome == outcome)
        if cursor:
            if not cursor.isdigit():
                raise ValidationFailed("Invalid cursor", errors=[{"field": "cursor", "message": "Invalid"}])
            stmt = stmt.where(ImportRow.row_number > int(cursor))
        found = list(await self.s.scalars(stmt.order_by(ImportRow.row_number).limit(limit + 1)))
        nxt = str(found[limit - 1].row_number) if len(found) > limit else None
        return found[:limit], nxt, await self.counts(job_id)

    async def report_csv(self, job_id: uuid.UUID) -> str:
        await self._import_job(job_id)
        rows = await self.s.scalars(
            select(ImportRow).where(ImportRow.job_id == job_id).order_by(ImportRow.row_number)
        )
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["row", "vendor", "outcome", "vendor_id", "proposal_id", "messages"])
        for r in rows:
            writer.writerow(
                [
                    r.row_number,
                    _csv_safe(r.vendor_name),
                    r.outcome.value,
                    r.vendor_id or "",
                    r.proposal_id or "",
                    _csv_safe(" | ".join(r.messages)),
                ]
            )
        return out.getvalue()


def _csv_safe(value: str) -> str:
    """Neutralises spreadsheet formula injection: the report reopens in Excel, and the cells came from a file."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value
