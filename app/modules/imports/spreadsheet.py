"""Reading uploaded spreadsheets (.xlsx/.xlsm via openpyxl, .csv) into plain text grids, and suggesting a column
mapping from the header names.

Everything here is synchronous and CPU-bound: callers run it with `anyio.to_thread.run_sync`.
"""

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.core.exceptions import ValidationFailed
from app.modules.imports.docx import read_docx_tables

SPREADSHEET_EXTENSIONS = {".xlsx", ".xlsm", ".csv"}
TABLE_EXTENSIONS = SPREADSHEET_EXTENSIONS | {".docx"}  # Word tables read as sheets (MEL import)
HEADER_SCAN_ROWS = 10
MAX_COLUMNS = 60
MAX_CELL_CHARS = 5000


@dataclass
class Sheet:
    name: str
    rows: list[list[str]]  # every row padded to the same width; row i is spreadsheet row i + 1
    truncated: bool = False  # more rows than the caller's limit; the rest were not read

    @property
    def width(self) -> int:
        return len(self.rows[0]) if self.rows else 0


@dataclass
class Workbook:
    sheets: list[Sheet] = field(default_factory=list)

    def sheet(self, name: str | None) -> Sheet:
        if name is None:
            return self.sheets[0]
        for sheet in self.sheets:
            if sheet.name == name:
                return sheet
        raise ValidationFailed(
            f"The file has no sheet named {name!r}",
            errors=[
                {"field": "sheet", "message": f"unknown sheet; choose one of {[s.name for s in self.sheets]}"}
            ],
        )


def extension(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot >= 0 else ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float | Decimal) and value == int(value):
        return str(int(value))
    return str(value).strip()[:MAX_CELL_CHARS]


def _grid(raw_rows: list[list[str]]) -> list[list[str]]:
    """Pads rows to a common width and drops trailing empty columns and rows."""
    width = max((max((i + 1 for i, v in enumerate(r) if v), default=0) for r in raw_rows), default=0)
    width = min(width, MAX_COLUMNS)
    rows = [(r + [""] * width)[:width] for r in raw_rows]
    while rows and not any(rows[-1]):
        rows.pop()
    return rows


def _read_xlsx(data: bytes, max_rows: int) -> Workbook:
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ValidationFailed("The file isn't a readable Excel workbook (.xlsx)") from exc
    book = Workbook()
    try:
        for ws in wb.worksheets:
            raw: list[list[str]] = []
            truncated = False
            for values in ws.iter_rows(values_only=True, max_col=MAX_COLUMNS):
                if len(raw) >= max_rows:
                    truncated = True
                    break
                raw.append([_text(v) for v in values])
            book.sheets.append(Sheet(name=ws.title, rows=_grid(raw), truncated=truncated))
    finally:
        wb.close()
    return book


def _read_csv(data: bytes, max_rows: int) -> Workbook:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes nearly anything
        raise ValidationFailed("The CSV file's text encoding isn't supported; save it as UTF-8")
    # The header line decides the delimiter (Excel in many locales saves ";"-separated CSV); csv.Sniffer is
    # easily misled by delimiters inside data cells.
    first = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = max(",;\t|", key=first.count) if first else ","
    raw: list[list[str]] = []
    truncated = False
    for values in csv.reader(io.StringIO(text), delimiter=delimiter):
        if len(raw) >= max_rows:
            truncated = True
            break
        raw.append([_text(v) for v in values[:MAX_COLUMNS]])
    return Workbook(sheets=[Sheet(name="Sheet1", rows=_grid(raw), truncated=truncated)])


def read_spreadsheet(data: bytes, filename: str, *, max_rows: int) -> Workbook:
    """Reads at most `max_rows` rows per sheet (header rows included)."""
    ext = extension(filename)
    if ext == ".csv":
        book = _read_csv(data, max_rows)
    elif ext in {".xlsx", ".xlsm"}:
        book = _read_xlsx(data, max_rows)
    elif ext == ".docx":
        book = Workbook(
            sheets=[
                Sheet(
                    name=name, rows=_grid([[_text(v) for v in r[:MAX_COLUMNS]] for r in rows]), truncated=cut
                )
                for name, rows, cut in read_docx_tables(data, max_rows)
            ]
        )
        if not book.sheets:
            raise ValidationFailed("The Word document has no tables to read")
    else:
        raise ValidationFailed(
            f"Unsupported file type {ext or '(none)'}; upload .xlsx or .csv "
            "(old .xls files: open in Excel and save as .xlsx)"
        )
    if not book.sheets:
        raise ValidationFailed("The workbook has no sheets")
    return book


def detect_header_row(sheet: Sheet) -> int:
    """1-based number of the first row that looks like a header (2+ filled cells, mostly text)."""
    for idx, row in enumerate(sheet.rows[:HEADER_SCAN_ROWS]):
        filled = [v for v in row if v]
        texty = [v for v in filled if not re.fullmatch(r"[\d.,\-/: ]+", v)]
        if len(filled) >= 2 and len(texty) >= len(filled) / 2:
            return idx + 1
    return 1


def headers(sheet: Sheet, header_row: int) -> list[str]:
    if header_row < 1 or header_row > max(len(sheet.rows), 1):
        raise ValidationFailed(
            "header_row is outside the sheet",
            errors=[{"field": "header_row", "message": f"the sheet has {len(sheet.rows)} rows"}],
        )
    if not sheet.rows:
        return []
    return [" ".join(v.split()) or f"Column {i + 1}" for i, v in enumerate(sheet.rows[header_row - 1])]


# ---------------------------------------------------------------- column mapping

# Target fields of a vendor-list import, with header names commonly used for them (compared after
# lower-casing and stripping punctuation). Order matters: the first field to claim a column wins.
FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": (
        "vendor name",
        "vendor",
        "supplier name",
        "supplier",
        "company name",
        "company",
        "manufacturer",
        "manufacturer name",
        "name",
        "legal name",
        "vendor legal name",
        "organisation",
        "organization",
    ),
    "website": ("website", "web site", "url", "web", "homepage", "web address", "domain", "site"),
    "country": ("country", "hq country", "headquarters country", "country of origin", "nation"),
    "city": ("city", "hq city", "town", "location"),
    "description": ("description", "about", "profile", "company description", "remarks"),
    "aliases": ("aliases", "alias", "also known as", "aka", "trade name", "trade names", "other names"),
    "equipment": (
        "equipment",
        "products",
        "product",
        "product category",
        "equipment category",
        "category",
        "categories",
        "scope of supply",
        "product line",
        "commodity",
        "commodities",
    ),
    "services": ("services", "service", "service category", "service categories"),
    "certifications": ("certifications", "certification", "certificates", "approvals", "standards"),
    "industries": ("industries", "industry", "sector", "sectors", "market", "markets"),
    "internal_code": (
        "vendor code",
        "supplier code",
        "vendor id",
        "supplier id",
        "vendor number",
        "supplier number",
        "internal code",
        "code",
        "erp code",
        "sap code",
    ),
    "avl_status": (
        "avl status",
        "avl",
        "approval status",
        "approved",
        "status",
        "vendor status",
        "qualified",
    ),
    "tags": ("tags", "tag", "labels", "group", "vendor group"),
    "notes": ("notes", "note", "comments", "comment"),
}
FIELDS = tuple(FIELD_SYNONYMS)


def _norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def suggest_mapping(header_names: list[str]) -> dict[str, int]:
    """field -> 0-based column index, for headers that clearly name a field. Exact synonyms first, then
    headers that contain a synonym as whole words (e.g. "Main Products / Equipment")."""
    normed = [_norm_header(h) for h in header_names]
    mapping: dict[str, int] = {}
    taken: set[int] = set()
    for exact in (True, False):
        for fld, synonyms in FIELD_SYNONYMS.items():
            if fld in mapping:
                continue
            for synonym in synonyms:
                for idx, header in enumerate(normed):
                    if idx in taken or not header:
                        continue
                    hit = header == synonym if exact else re.search(rf"\b{re.escape(synonym)}\b", header)
                    if hit:
                        mapping[fld] = idx
                        taken.add(idx)
                        break
                if fld in mapping:
                    break
    return mapping


def data_rows(sheet: Sheet, header_row: int, limit: int) -> tuple[list[tuple[int, list[str]]], bool]:
    """Non-empty rows below the header as (spreadsheet row number, cells), at most `limit` of them, and whether
    any were left out."""
    out: list[tuple[int, list[str]]] = []
    for idx in range(header_row, len(sheet.rows)):
        values = sheet.rows[idx]
        if not any(values):
            continue
        if len(out) >= limit:
            return out, True
        out.append((idx + 1, values))
    return out, sheet.truncated
