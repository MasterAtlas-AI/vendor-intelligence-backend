"""Reading Word (.docx) files without extra dependencies: a .docx is a zip with word/document.xml.

Tables become sheets (for MELs and vendor lists kept in Word); all text becomes pages (for vendor documents, read
like a PDF). Old binary .doc files aren't supported: save them as .docx first.
"""

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from app.core.exceptions import ValidationFailed

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CHARS_PER_PAGE = 3000  # a Word file has no fixed pages; text is cut into page-sized chunks for the extractor


def _document(data: bytes) -> ET.Element:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValidationFailed(
            "The file isn't a readable Word document (.docx); save it as .docx and retry"
        ) from exc
    if len(xml) > 50_000_000:
        raise ValidationFailed("The Word document is too large")
    return ET.fromstring(xml)  # noqa: S314 - the archive is size-checked; ElementTree doesn't expand external entities


def _text(el: ET.Element) -> str:
    parts: list[str] = []
    for node in el.iter():
        if node.tag == f"{W}t" and node.text:
            parts.append(node.text)
        elif node.tag in (f"{W}tab",):
            parts.append(" ")
        elif node.tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    return re.sub(r"[ \t]+", " ", "".join(parts)).strip()


def read_docx_tables(data: bytes, max_rows: int) -> list[tuple[str, list[list[str]], bool]]:
    """Every table as (name, rows, truncated). Merged cells repeat nothing: a missing cell is an empty string."""
    body = _document(data)
    tables: list[tuple[str, list[list[str]], bool]] = []
    for i, table in enumerate(body.iter(f"{W}tbl"), start=1):
        rows: list[list[str]] = []
        truncated = False
        for tr in table.iter(f"{W}tr"):
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append([_text(tc) for tc in tr.findall(f"{W}tc")])
        if any(any(cell for cell in row) for row in rows):
            tables.append((f"Table {i}", rows, truncated))
    return tables


def read_docx_pages(data: bytes, max_pages: int) -> tuple[list[str], int]:
    """All paragraph text (tables included), cut into page-sized chunks; returns (pages, total page count)."""
    body = _document(data)
    paragraphs = [_text(p) for p in body.iter(f"{W}p")]
    text = "\n".join(p for p in paragraphs if p)
    chunks = [text[i : i + CHARS_PER_PAGE] for i in range(0, len(text), CHARS_PER_PAGE)] or [""]
    return chunks[:max_pages], len(chunks)
