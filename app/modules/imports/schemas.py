import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import AvlStatus, ImportRowOutcome, UploadKind
from app.modules.imports.spreadsheet import FIELDS


class SheetInfo(BaseModel):
    name: str
    header_row: int  # detected; 1-based
    headers: list[str]
    data_rows: int  # rows below the header (capped at the import limit)
    truncated: bool  # the sheet has more rows than one import accepts
    sample: list[list[str]]  # the first few rows below the header
    suggested_mapping: dict[str, int]  # field -> 0-based column index


class UploadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: UploadKind
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
    sheets: list[SheetInfo] = Field(default_factory=list)  # spreadsheets
    pdf: "PdfInfo | None" = None  # PDFs


class ImportOptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_avl_status: AvlStatus | None = None  # for rows without an AVL status column/value
    tags: list[str] = Field(default_factory=list, max_length=10)  # added to every imported vendor

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return [t.strip()[:60] for t in v if t.strip()]


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_id: uuid.UUID
    sheet: str | None = None  # default: the first sheet
    header_row: int | None = Field(default=None, ge=1, le=50)  # default: detected
    mapping: dict[str, int] = Field(description="Field -> 0-based column index. `name` is required.")
    options: ImportOptionsIn = Field(default_factory=ImportOptionsIn)

    @model_validator(mode="after")
    def _mapping(self) -> "ImportRequest":
        unknown = sorted(set(self.mapping) - set(FIELDS))
        if unknown:
            raise ValueError(f"unknown mapping field(s) {unknown}; allowed: {list(FIELDS)}")
        if "name" not in self.mapping:
            raise ValueError("map a column to `name` (the vendor name)")
        if any(i < 0 for i in self.mapping.values()):
            raise ValueError("column indexes start at 0")
        if len(set(self.mapping.values())) != len(self.mapping):
            raise ValueError("each column can be mapped to one field only")
        return self


class PreviewRow(BaseModel):
    row_number: int
    values: dict[str, str]  # mapped field -> raw cell text
    outcome: ImportRowOutcome  # predicted
    vendor_id: uuid.UUID | None
    messages: list[str]


class ImportPreview(BaseModel):
    sheet: str
    header_row: int
    headers: list[str]
    data_rows: int
    rows: list[PreviewRow]
    predicted: dict[str, int]  # outcome -> count, for the previewed rows only


class ImportRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    row_number: int
    outcome: ImportRowOutcome
    vendor_name: str
    vendor_id: uuid.UUID | None
    proposal_id: uuid.UUID | None
    overlay: dict[str, Any]
    messages: list[str]


class ImportRowPage(BaseModel):
    items: list[ImportRowRead]
    next_cursor: str | None
    has_more: bool
    counts: dict[str, int]  # outcome -> count for the whole import


# ---------------------------------------------------------------- PDF import


class PdfInfo(BaseModel):
    page_count: int
    pages_read: int  # capped at PDF_MAX_PAGES
    scanned: bool  # no usable text layer (OCR isn't supported yet)
    facts_found: int
    suggested_names: list[str]  # company names with a legal form, most frequent first
    suggested_domains: list[str]  # websites/e-mail domains mentioned, most frequent first


class PdfVendor(BaseModel):
    """Whose document this is: an existing vendor, or a new one described by name (and ideally website)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    vendor_id: uuid.UUID | None = None
    name: str | None = Field(default=None, max_length=300)
    website: str | None = Field(default=None, max_length=500)
    country: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def _identity(self) -> "PdfVendor":
        if self.vendor_id is None and not self.name:
            raise ValueError("give vendor_id (an existing vendor) or the vendor's name")
        return self


class PdfImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_id: uuid.UUID
    vendor: PdfVendor | None = None  # optional for the preview, required to import
    include: list[str] | None = Field(
        default=None, max_length=500, description="Candidate keys to propose; default: all found"
    )
    options: ImportOptionsIn = Field(default_factory=ImportOptionsIn)


class PdfCandidate(BaseModel):
    key: str  # "<kind>:<code>", used in `include`
    kind: str  # equipment | services | certifications | industries
    code: str
    name: str
    page: int
    snippet: str
    occurrences: int
    confidence: Decimal
    details: dict[str, Any]  # certificate_number / valid_to, when found next to a certification


class PdfVendorMatch(BaseModel):
    outcome: (
        ImportRowOutcome  # predicted: matched / proposed / created / already_proposed / ambiguous / invalid
    )
    vendor_id: uuid.UUID | None
    messages: list[str]


class PdfPreview(PdfInfo):
    candidates: list[PdfCandidate]
    vendor: PdfVendorMatch | None  # only when the request named the vendor


UploadRead.model_rebuild()  # resolves the forward reference to PdfInfo
