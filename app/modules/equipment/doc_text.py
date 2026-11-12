"""Text and ratings read from a project document, on the server (no AI): the words of a report or letter for search
(SOW H.4 / 25.4), and "label: value unit" pairs of a datasheet or specification for the equipment line it belongs
to (SOW 3.11 / 3.12 / 8.4).

PDF and Word text, spreadsheet (and Word table) cells, plain text and e-mail. Drawings, scans and anything else
give no text; that is fine, the document stays in the register.
"""

import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from app.modules.equipment.extract import extract_spec_attributes
from app.modules.imports.docx import read_docx_tables
from app.modules.imports.pdf_extract import read_document
from app.modules.imports.spreadsheet import extension, read_spreadsheet

MAX_PAGES = 80
MAX_ROWS = 2000
MAX_TEXT = 400_000  # characters kept per document
_TEXT_TYPES = {".txt", ".csv", ".eml", ".md", ".log"}


@dataclass
class DocText:
    text: str
    rows: list[list[str]]  # table cells (spreadsheets, Word tables), for label / value pairs


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def read_text(data: bytes, filename: str) -> DocText:
    """The document's words and table rows; empty when the file type has none or the file can't be read."""
    ext = extension(filename)
    text, rows = "", []
    try:
        if ext in {".xlsx", ".xlsm", ".csv"}:
            book = read_spreadsheet(data, filename, max_rows=MAX_ROWS)
            rows = [r for s in book.sheets for r in s.rows if any(c.strip() for c in r)]
            text = "\n".join(" | ".join(c for c in r if c.strip()) for r in rows)
        elif ext in {".pdf", ".docx"}:
            text = "\n".join(read_document(data, filename, max_pages=MAX_PAGES).pages)
            if ext == ".docx":
                rows = [r for _, table, _ in read_docx_tables(data, MAX_ROWS) for r in table]
        elif ext in _TEXT_TYPES:
            text = _decode(data)
    except Exception:  # a broken or protected file: no text, the document itself is kept
        return DocText("", [])
    text = re.sub(r"[ \t\x00]+", " ", text).strip()[:MAX_TEXT]
    return DocText(text, rows)


# ---------------------------------------------------------------- ratings

_UNIT_IN_LABEL = re.compile(r"[\(\[]\s*([^)\]]{1,20})\s*[\)\]]\s*$")
_PAIR = re.compile(r"^\s*([A-Za-z][^:=\t|]{1,80}?)\s*(?::|=|\t|\||\s{3,}|\.{3,})\s*(.{1,120}?)\s*$")


def _with_unit(label: str, value: str) -> str:
    """'Capacity (m3/h)' + '120' reads as '120 m3/h'."""
    unit = _UNIT_IN_LABEL.search(label)
    if unit and re.fullmatch(r"[\d.,\s~-]+", value.strip()):
        return f"{value.strip()} {unit.group(1)}"
    return value


def spec_pairs(doc: DocText) -> dict[str, str]:
    """Label / value pairs: a cell and the next non-empty cell of a table row, and 'Label: value' lines of text."""
    pairs: dict[str, str] = {}

    def add(label: str, value: str) -> None:
        label, value = " ".join(label.split()), " ".join(value.split())
        if label and value and re.search(r"[A-Za-z]", label) and re.search(r"\d", value):
            pairs.setdefault(label, _with_unit(label, value))

    for row in doc.rows:
        cells = [c for c in row if c and c.strip()]
        for label, value in pairwise(cells):
            if not re.search(r"\d", label) or re.match(r"[A-Za-z]", label):
                add(label, value)
    for line in doc.text.splitlines():
        m = _PAIR.match(line)
        if m:
            add(m.group(1), m.group(2))
    return pairs


def read_ratings(doc: DocText) -> tuple[dict[str, list[float]], dict[str, str]]:
    """The numeric ratings found (attribute key -> [min, max]) and, per key, the line they came from."""
    pairs = spec_pairs(doc)
    found: dict[str, list[float]] = {}
    source: dict[str, str] = {}
    for label, value in pairs.items():
        for key, span in extract_spec_attributes({label: value}).items():
            if key not in found:
                found[key], source[key] = span, f"{label}: {value}"[:200]
    return found, source


def as_value(span: list[float]) -> Any:
    """The stored form: a number, or [min, max] for a range."""
    low, high = span
    return low if low == high else [low, high]
