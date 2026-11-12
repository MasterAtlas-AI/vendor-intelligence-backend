"""Rule-based fact extraction from vendor PDFs (brochures, datasheets, certificates, prequalification packs).

No AI here (that arrives in Phase 3): the text layer is read with pypdf and matched against the taxonomy's names
and synonyms. Every hit keeps the page and a snippet of the surrounding text as evidence, so curators can check it.
Scanned PDFs without a text layer are detected and reported; OCR is out of scope for now.

Everything here is synchronous and CPU-bound: callers run it with `anyio.to_thread.run_sync`.
"""

import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from pypdf import PdfReader

from app.core.exceptions import ValidationFailed
from app.domain.normalization import normalize_name, registrable_domain
from app.modules.imports.docx import read_docx_pages

MAX_PAGE_CHARS = 200_000
SNIPPET_BEFORE, SNIPPET_AFTER = 120, 200
CERT_DETAIL_WINDOW = 250  # characters after a certification mention searched for its number and expiry
# Average characters per page below which the PDF is treated as scanned (no usable text layer).
SCANNED_CHARS_PER_PAGE = 40
EN_DASH = chr(0x2013)

# Kinds of fact extracted, with the taxonomy each is matched against.
KINDS = ("equipment", "services", "certifications", "industries")


# ---------------------------------------------------------------- reading


@dataclass
class PdfText:
    pages: list[str]  # page i is page number i + 1
    page_count: int  # pages in the document (may exceed len(pages) when truncated)
    scanned: bool

    @property
    def truncated(self) -> bool:
        return self.page_count > len(self.pages)


def read_document(data: bytes, filename: str, *, max_pages: int) -> PdfText:
    """A vendor document's text: a PDF, or a Word file (.docx) read the same way."""
    if filename.lower().endswith(".docx"):
        pages, count = read_docx_pages(data, max_pages)
        return PdfText(pages=[p[:MAX_PAGE_CHARS] for p in pages], page_count=count, scanned=False)
    return read_pdf(data, max_pages=max_pages)


def read_pdf(data: bytes, *, max_pages: int) -> PdfText:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValidationFailed("The PDF is password-protected; upload an unprotected copy")
        page_count = len(reader.pages)
    except ValidationFailed:
        raise
    except Exception as exc:  # pypdf raises many types for malformed files
        raise ValidationFailed("The file isn't a readable PDF") from exc
    pages: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            text = page.extract_text() or ""
        except Exception:  # one broken page shouldn't sink the document
            text = ""
        pages.append(text[:MAX_PAGE_CHARS])
    chars = sum(len(re.sub(r"\s+", "", p)) for p in pages)
    scanned = chars < SCANNED_CHARS_PER_PAGE * max(len(pages), 1)
    return PdfText(pages=pages, page_count=page_count, scanned=scanned)


# ---------------------------------------------------------------- tokenising

_TOKEN = re.compile(r"[a-z]+|\d+")


def tokens(text: str) -> list[tuple[str, int]]:
    """Lower-cased word tokens with their character offsets. Short prefixes are glued to a following number
    ("BB-2", "BB 2", "bb2" -> "bb2"; "ISO 9001" -> "iso9001"), and plurals are folded ("pumps" -> "pump"), so
    labels and text compare the same way regardless of spacing. "and" is dropped, so it matches "&"."""
    raw = [(m.group(), m.start(), m.end()) for m in _TOKEN.finditer(text.lower())]
    out: list[tuple[str, int]] = []
    i = 0
    while i < len(raw):
        tok, start, end = raw[i]
        if tok.isalpha() and len(tok) <= 4 and i + 1 < len(raw):
            nxt, nstart, _ = raw[i + 1]
            if nxt.isdigit() and nstart - end <= 1 and text[end:nstart] in ("", "-", "_", " "):
                out.append((tok + nxt, start))
                i += 2
                continue
        if tok == "and":  # "shell and tube" = "Shell & tube" ("&" is never a token)
            i += 1
            continue
        if tok.isalpha() and len(tok) > 3 and tok.endswith("s"):
            tok = tok[:-1]
        out.append((tok, start))
        i += 1
    return out


def label_variants(label: str) -> list[str]:
    """The ways a taxonomy name is likely to appear in running text: the full name, and its short forms
    ("BB2 - Between bearings, radially split" -> "BB2"; "API 610 / ISO 13709 process pumps" -> both halves)."""
    variants = {label.strip()}
    head = re.split(r"\s+[-" + EN_DASH + r"]\s+", label, maxsplit=1)[0]
    variants.add(head.strip())
    for part in re.split(r"\s*/\s*", head):
        variants.add(part.strip())
    return [v for v in variants if v]


def _usable(kind: str, label: str, toks: tuple[str, ...]) -> bool:
    """Single generic words ("Pumps", "Offshore", "Power") match far too much prose. A one-word label counts
    only if it carries a number ("BB2", "ISO 9001") or, for certifications and industries, is an acronym
    ("ATEX", "FPSO")."""
    if len(toks) >= 2:
        return True
    if any(ch.isdigit() for ch in toks[0]):
        return len(toks[0]) >= 3
    return kind in ("certifications", "industries") and label.isupper() and len(label) >= 3


# ---------------------------------------------------------------- matching


@dataclass
class Hit:
    kind: str
    code: str
    page: int  # 1-based, first occurrence
    snippet: str
    occurrences: int = 1
    details: dict[str, Any] = field(default_factory=dict)  # certificate number / validity, when found

    @property
    def confidence(self) -> Decimal:
        """Rough: a fact mentioned repeatedly is more likely to be what the vendor actually offers."""
        base = Decimal("0.75") if self.kind == "certifications" else Decimal("0.55")
        return min(Decimal("0.95"), base + Decimal("0.1") * (min(self.occurrences, 5) - 1))


@dataclass
class Extraction:
    hits: list[Hit]
    domains: list[tuple[str, int]]  # (registrable domain, mentions), most frequent first
    names: list[tuple[str, int]]  # likely company names (with a legal form), most frequent first


class PhraseIndex:
    """Taxonomy labels as token sequences, indexed by their first token."""

    def __init__(self, labels: dict[str, list[tuple[str, list[str]]]]) -> None:
        codes_by_phrase: dict[tuple[str, tuple[str, ...]], set[str]] = {}
        for kind in KINDS:
            for code, names in labels.get(kind, []):
                for name in names:
                    for variant in label_variants(name):
                        toks = tuple(t for t, _ in tokens(variant))
                        if toks and _usable(kind, variant, toks):
                            codes_by_phrase.setdefault((kind, toks), set()).add(code)
        self.by_first: dict[str, list[tuple[str, tuple[str, ...], str]]] = {}
        for (kind, toks), codes in codes_by_phrase.items():
            if len(codes) == 1:  # a phrase naming several categories says nothing specific
                self.by_first.setdefault(toks[0], []).append((kind, toks, next(iter(codes))))
        for entries in self.by_first.values():
            entries.sort(key=lambda e: -len(e[1]))  # longest phrase first

    def find(self, text: str) -> list[tuple[str, str, int, int]]:
        """(kind, code, start offset, end offset) for every phrase occurrence in the text."""
        toks = tokens(text)
        out: list[tuple[str, str, int, int]] = []
        for i, (tok, start) in enumerate(toks):
            for kind, phrase, code in self.by_first.get(tok, ()):
                n = len(phrase)
                if tuple(t for t, _ in toks[i : i + n]) == phrase:
                    end = toks[i + n][1] if i + n < len(toks) else len(text)
                    out.append((kind, code, start, end))
        return out


def _snippet(text: str, start: int) -> str:
    raw = text[max(0, start - SNIPPET_BEFORE) : start + SNIPPET_AFTER]
    return re.sub(r"\s+", " ", raw).strip()


_CERT_NO = re.compile(
    r"(?:certificate|cert\.?|registration|reg\.?)\s*(?:no\.?|number|#)\s*[:.#]?\s*([A-Z0-9][A-Z0-9\-/.]{2,40}[A-Z0-9])",
    re.IGNORECASE,
)
_VALID = re.compile(
    r"(?:valid\s+(?:until|till|to|through|thru)|expir(?:y|es|ation)(?:\s+date)?)\s*[:.]?\s*", re.I
)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}  # fmt: skip


def parse_date(text: str) -> date | None:
    """The date at the start of `text`: 2027-03-31, 31.03.2027, 31/03/2027 (day first) or 31 March 2027."""
    text = text.strip()
    if m := re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    elif m := re.match(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text):
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
    elif m := re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", text):
        mo_ = _MONTHS.get(m[2][:3].lower())
        if mo_ is None:
            return None
        d, mo, y = int(m[1]), mo_, int(m[3])
    else:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _cert_details(text: str, end: int) -> dict[str, Any]:
    window = text[end : end + CERT_DETAIL_WINDOW]
    details: dict[str, Any] = {}
    if m := _CERT_NO.search(window):
        details["certificate_number"] = m[1][:120]
    if m := _VALID.search(window):
        if valid := parse_date(window[m.end() :]):
            details["valid_to"] = valid.isoformat()
    return details


def _drop_ancestors(hits: dict[tuple[str, str], Hit]) -> None:
    """For tree taxonomies, "pumps.centrifugal" adds nothing when "pumps.centrifugal.api610.bb2" was also found."""
    for kind in ("equipment", "services"):
        codes = [c for k, c in hits if k == kind]
        for code in codes:
            if any(other.startswith(code + ".") for other in codes):
                hits.pop((kind, code), None)


_URL = re.compile(r"https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+|[\w.+-]+@(?:[\w-]+\.)+[a-z]{2,}", re.I)
_LEGAL_NAME = re.compile(
    # 1-5 capitalised (or numeric) words, lower-case connectors allowed after the first ("Flow and Seal",
    # "Bureau van Dijk"), then a legal form.
    r"\b([A-Z0-9][\w&'.-]*\s(?:(?:[A-Z0-9&][\w&'.-]*|and|of|de|du|la|van|von|der|den|del)\s){0,4}?"
    r"(?:Ltd\.?|Limited|GmbH|AG|ASA|AS|A/S|AB|Oy|Inc\.?|LLC|Corp\.?|Corporation|"
    r"S\.?A\.?|S\.?p\.?A\.?|S\.?r\.?l\.?|B\.?V\.?|N\.?V\.?|Pvt\.? Ltd\.?|Pte\.? Ltd\.?|Co\.,? Ltd\.?|PLC|plc|SE|KG))"
    r"(?=[\s,.;)]|$)"
)


def extract(pdf: PdfText, index: PhraseIndex) -> Extraction:
    hits: dict[tuple[str, str], Hit] = {}
    domains: Counter[str] = Counter()
    names: Counter[str] = Counter()
    display: dict[str, str] = {}
    for page_no, text in enumerate(pdf.pages, start=1):
        if not text.strip():
            continue
        for kind, code, start, end in index.find(text):
            hit = hits.get((kind, code))
            if hit is None:
                hits[(kind, code)] = hit = Hit(kind, code, page_no, _snippet(text, start))
            else:
                hit.occurrences += 1
            if kind == "certifications":
                for k, v in _cert_details(text, end).items():
                    hit.details.setdefault(k, v)
        for m in _URL.finditer(text):
            value = m.group().split("@")[-1]
            if domain := registrable_domain(value.rstrip(".,;:")):
                domains[domain] += 1
        for m in _LEGAL_NAME.finditer(text):
            name = re.sub(r"\s+", " ", m[1]).strip()
            key = normalize_name(name)
            if key and len(key) >= 3:
                names[key] += 1
                display.setdefault(key, name)
    _drop_ancestors(hits)
    order = {k: i for i, k in enumerate(KINDS)}
    return Extraction(
        hits=sorted(hits.values(), key=lambda h: (order[h.kind], h.page, h.code)),
        domains=domains.most_common(5),
        names=[(display[k], n) for k, n in names.most_common(5)],
    )
