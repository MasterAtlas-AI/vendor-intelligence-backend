"""Builds small text PDFs for tests (no PDF-writing dependency) and taxonomy labels from the seed data."""

from app.modules.imports.importer import text_labels
from app.modules.taxonomy.seed import flatten
from app.modules.taxonomy.seed_data import CERTIFICATIONS, EQUIPMENT, INDUSTRIES, SERVICES


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]]) -> bytes:
    """A valid PDF with one Helvetica text line per string, one page per list."""
    objects: list[bytes] = []
    kids = []
    font_id = 3
    for i, lines in enumerate(pages):
        content = "BT /F1 10 Tf 40 800 Td 14 TL " + " ".join(f"({_escape(ln)}) Tj T*" for ln in lines) + " ET"
        page_id, content_id = 4 + 2 * i, 5 + 2 * i
        kids.append(page_id)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream".encode())
    header = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(header + objects, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def seed_labels() -> dict[str, list[tuple[str, list[str]]]]:
    """Same as Taxonomy.labels after `bootstrap`, built from seed_data (for tests without a database)."""
    return {
        "equipment": [
            (c, text_labels("equipment", c, n.name, n.synonyms)) for c, _, _, n in flatten(EQUIPMENT)
        ],
        "services": [(c, text_labels("services", c, n.name, n.synonyms)) for c, _, _, n in flatten(SERVICES)],
        "certifications": [
            (c, text_labels("certifications", c, name, ())) for c, name, _, _ in CERTIFICATIONS
        ],
        "industries": [(c, text_labels("industries", c, name, syn)) for c, name, syn in INDUSTRIES],
    }
