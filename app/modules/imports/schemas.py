import uuid
from datetime import datetime
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
    sheets: list[SheetInfo] = Field(default_factory=list)


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
