"""PDF import: one document about one vendor -> facts proposed for that vendor, with page-level evidence.

The user says whose document it is (an existing vendor, or a new one by name/website); the extractor finds
candidate facts; the user may deselect some (`include`). The rest goes through the same importer as a spreadsheet
row, so matching, duplicate checks, auto-approval for trusted tenants and the tenant's own list all behave alike.
"""

import uuid
from typing import Any

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.domain.enums import ProposalItemAction, SourceKind, VendorStatus
from app.modules.imports.importer import ImportOptions, ParsedRow, Taxonomy, _fact_key, parse_row
from app.modules.imports.pdf_extract import Extraction, Hit, PdfText, PhraseIndex, extract, read_pdf
from app.modules.imports.schemas import PdfCandidate, PdfInfo, PdfVendor
from app.modules.vendors.models import Vendor

DOCUMENT_TITLE = (
    "Vendor document (PDF)"  # deliberately not the filename: which tenant uploaded what is private
)
DOCUMENT_EVIDENCE = [{"kind": SourceKind.DOCUMENT.value, "title": DOCUMENT_TITLE}]

_ACTIONS = {
    "equipment": ProposalItemAction.ADD_EQUIPMENT,
    "services": ProposalItemAction.ADD_SERVICE,
    "certifications": ProposalItemAction.ADD_CERTIFICATION,
    "industries": ProposalItemAction.ADD_INDUSTRY,
}


def candidate_key(hit: Hit) -> str:
    return f"{hit.kind}:{hit.code}"


async def read_and_extract(data: bytes, taxonomy: Taxonomy, max_pages: int) -> tuple[PdfText, Extraction]:
    def work() -> tuple[PdfText, Extraction]:
        pdf = read_pdf(data, max_pages=max_pages)
        return pdf, extract(pdf, PhraseIndex(taxonomy.labels))

    return await anyio.to_thread.run_sync(work)


def pdf_info(pdf: PdfText, ex: Extraction) -> PdfInfo:
    return PdfInfo(
        page_count=pdf.page_count,
        pages_read=len(pdf.pages),
        scanned=pdf.scanned,
        facts_found=len(ex.hits),
        suggested_names=[name for name, _ in ex.names],
        suggested_domains=[domain for domain, _ in ex.domains],
    )


def candidates(ex: Extraction, taxonomy: Taxonomy) -> list[PdfCandidate]:
    return [
        PdfCandidate(
            key=candidate_key(h),
            kind=h.kind,
            code=h.code,
            name=taxonomy.names.get((h.kind, h.code), h.code),
            page=h.page,
            snippet=h.snippet,
            occurrences=h.occurrences,
            confidence=h.confidence,
            details=h.details,
        )
        for h in ex.hits
    ]


async def vendor_row(
    session: AsyncSession,
    taxonomy: Taxonomy,
    vendor: PdfVendor,
    pdf: PdfText,
    ex: Extraction,
    include: list[str] | None,
    options: ImportOptions,
) -> ParsedRow:
    """The document as one import row: the vendor's identity plus the selected facts and their evidence."""
    name, vendor_id = vendor.name or "", None
    if vendor.vendor_id:
        existing = await session.get(Vendor, vendor.vendor_id)
        if existing is None or existing.status == VendorStatus.RETIRED:
            raise NotFound("Vendor not found")
        name, vendor_id = existing.display_name, existing.id
    row = parse_row(
        1,
        [name, vendor.website or "", vendor.country or ""],
        {"name": 0, "website": 1, "country": 2},
        taxonomy,
        options,
    )
    row.vendor_id = vendor_id
    if pdf.scanned:
        row.messages.append(
            "The PDF has little or no text (scanned?); facts can only be read from a text layer"
        )
    if pdf.truncated:
        row.messages.append(f"Only the first {len(pdf.pages)} of {pdf.page_count} pages were read")

    wanted = set(include) if include is not None else None
    for hit in ex.hits:
        if wanted is not None and candidate_key(hit) not in wanted:
            continue
        action = _ACTIONS[hit.kind]
        if hit.kind in ("equipment", "services"):
            entry = (hit.code, taxonomy.names.get((hit.kind, hit.code), hit.code)[:300])
            (row.equipment if hit.kind == "equipment" else row.services).append(entry)
            payload: dict[str, Any] = {"category_code": hit.code}
        elif hit.kind == "certifications":
            row.certifications.append(hit.code)
            payload = {"certification_code": hit.code}
            if hit.details:
                row.extra[_fact_key(action, payload)] = dict(hit.details)
        else:
            row.industries.append(hit.code)
            payload = {"industry_code": hit.code}
        key = _fact_key(action, payload)
        row.evidence[key] = [
            {**DOCUMENT_EVIDENCE[0], "snippet": hit.snippet[:2000], "page": hit.page},
        ]
        row.confidence[key] = hit.confidence
    return row


def job_summary(
    upload_id: uuid.UUID, filename: str, pdf: PdfText, ex: Extraction, row: ParsedRow
) -> dict[str, Any]:
    return {
        "upload_id": str(upload_id),
        "filename": filename,
        "page_count": pdf.page_count,
        "pages_read": len(pdf.pages),
        "scanned": pdf.scanned,
        "facts_found": len(ex.hits),
        "facts_selected": len(row.equipment)
        + len(row.services)
        + len(row.certifications)
        + len(row.industries),
    }
