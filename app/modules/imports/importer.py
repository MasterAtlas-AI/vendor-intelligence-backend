"""Vendor-list import: turns spreadsheet rows into tenant vendor links and knowledge proposals.

For each row the importer:
  1. parses and validates the mapped cells (name, website, country, taxonomy terms, the tenant's own fields);
  2. finds the vendor in the global knowledge base: by website domain first, then by exact normalised name/alias;
  3. existing vendor -> adds it to the tenant's list (tenant_vendor_links) and proposes any facts the knowledge
     base doesn't have yet; unknown vendor -> proposes a new vendor (auto-approved for trusted tenants), and the
     vendor joins the tenant's list as soon as the proposal is approved (see `linking`);
  4. records the outcome in import_rows (the import report).

The same code runs as a dry run for the mapping preview (`dry_run=True`): nothing is written, outcomes are
predicted. The real run happens inside one transaction (see the job handler), so a crashed or cancelled import
leaves nothing behind and can simply be re-run.
"""

import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.domain.enums import (
    AvlStatus,
    FactStatus,
    ImportRowOutcome,
    ProposalItemAction,
    ProposalItemStatus,
    ProposalKind,
    ProposalOrigin,
    ProposalStatus,
    SourceKind,
    VendorStatus,
)
from app.domain.normalization import normalize_country, normalize_name, normalize_website, registrable_domain
from app.modules.imports.linking import upsert_overlay
from app.modules.imports.models import ImportRow
from app.modules.proposals.models import KnowledgeProposal, ProposalItem
from app.modules.proposals.service import NewItem, ProposalService
from app.modules.taxonomy.models import CertificationType, EquipmentCategory, Industry, ServiceCategory
from app.modules.vendors.models import (
    Vendor,
    VendorAlias,
    VendorCertification,
    VendorDomain,
    VendorEquipment,
    VendorIndustry,
    VendorProject,
    VendorService,
)

LIST_SPLIT = re.compile(r"\s*(?:[;|\n]|,(?!\d{3}))\s*")
EVIDENCE = [{"kind": SourceKind.EXCEL.value, "title": "Spreadsheet import"}]

AVL_SYNONYMS: dict[str, AvlStatus] = {
    "approved": AvlStatus.APPROVED,
    "yes": AvlStatus.APPROVED,
    "y": AvlStatus.APPROVED,
    "avl": AvlStatus.APPROVED,
    "qualified": AvlStatus.APPROVED,
    "conditional": AvlStatus.CONDITIONAL,
    "conditionally approved": AvlStatus.CONDITIONAL,
    "provisional": AvlStatus.CONDITIONAL,
    "not approved": AvlStatus.NOT_APPROVED,
    "no": AvlStatus.NOT_APPROVED,
    "n": AvlStatus.NOT_APPROVED,
    "rejected": AvlStatus.NOT_APPROVED,
    "disqualified": AvlStatus.NOT_APPROVED,
    "blacklisted": AvlStatus.BLACKLISTED,
    "blacklist": AvlStatus.BLACKLISTED,
    "banned": AvlStatus.BLACKLISTED,
    "blocked": AvlStatus.BLACKLISTED,
    "unassessed": AvlStatus.UNASSESSED,
    "pending": AvlStatus.UNASSESSED,
    "new": AvlStatus.UNASSESSED,
}


def split_list(cell: str) -> list[str]:
    seen: dict[str, None] = {}
    for part in LIST_SPLIT.split(cell):
        if part.strip():
            seen.setdefault(part.strip(), None)
    return list(seen)


def parse_avl(value: str) -> AvlStatus | None:
    key = re.sub(r"[\s_\-]+", " ", value.strip().lower())
    return AVL_SYNONYMS.get(key) or AVL_SYNONYMS.get(key.replace(" ", "_"))


# ---------------------------------------------------------------- taxonomy lookup


def _term_key(value: str) -> str:
    """Loose key for matching a cell to a taxonomy entry: case, punctuation, plurals and a trailing year
    ("ISO 9001:2015") are ignored, so "Centrifugal Pumps" == "centrifugal pump" and "ISO 9001" == "ISO_9001"."""
    value = re.sub(r"[:\-\s]\s*(19|20)\d\d$", "", value.strip().lower())
    words = re.findall(r"[a-z0-9]+", value)
    return "".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words)


def text_labels(kind: str, code: str, name: str, synonyms: list[str] | tuple[str, ...]) -> list[str]:
    """How a taxonomy entry may be written in running text (PDF matching). Certification codes are how documents
    usually name them ("ISO_9001" -> "ISO 9001", "API_Q1"); category codes are dot paths and never appear."""
    labels = [name, *synonyms]
    if kind == "certifications":
        labels.append(code.replace("_", " "))
    return labels


@dataclass
class _Vocabulary:
    by_key: dict[str, set[str]] = field(default_factory=dict)  # key -> codes

    def add(self, code: str, *labels: str) -> None:
        for label in labels:
            key = _term_key(label)
            if key:
                self.by_key.setdefault(key, set()).add(code)

    def resolve(self, term: str) -> str | None:
        codes = self.by_key.get(_term_key(term), set())
        return next(iter(codes)) if len(codes) == 1 else None


class Taxonomy:
    """In-memory lookup of the four taxonomies (a few hundred rows), loaded once per import."""

    def __init__(self) -> None:
        self.equipment = _Vocabulary()
        self.services = _Vocabulary()
        self.certifications = _Vocabulary()
        self.industries = _Vocabulary()
        self.ids: dict[tuple[str, str], uuid.UUID] = {}  # (kind, code) -> id
        self.names: dict[tuple[str, str], str] = {}  # (kind, code) -> display name
        self.labels: dict[str, list[tuple[str, list[str]]]] = {}  # kind -> [(code, [name, synonyms...])]

    @classmethod
    async def load(cls, session: AsyncSession) -> "Taxonomy":
        tax = cls()
        models: tuple[tuple[str, Any, _Vocabulary], ...] = (
            ("equipment", EquipmentCategory, tax.equipment),
            ("services", ServiceCategory, tax.services),
            ("certifications", CertificationType, tax.certifications),
            ("industries", Industry, tax.industries),
        )
        for kind, model, vocab in models:
            for row in await session.scalars(select(model)):
                vocab.add(row.code, row.code, row.name, *(row.synonyms or []))
                tax.ids[(kind, row.code)] = row.id
                tax.names[(kind, row.code)] = row.name
                tax.labels.setdefault(kind, []).append(
                    (row.code, text_labels(kind, row.code, row.name, row.synonyms or []))
                )
        return tax


# ---------------------------------------------------------------- parsing


@dataclass
class ParsedRow:
    row_number: int
    name: str = ""
    website: str | None = None
    domain: str | None = None
    country: str | None = None
    city: str | None = None
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    equipment: list[tuple[str, str]] = field(default_factory=list)  # (category code, the cell text)
    services: list[tuple[str, str]] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)  # codes
    industries: list[str] = field(default_factory=list)  # codes
    projects: list[dict[str, Any]] = field(default_factory=list)  # add_project payloads
    overlay: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    messages: list[str] = field(default_factory=list)
    # Set when the user picked the vendor explicitly (PDF import): no matching by name/website.
    vendor_id: uuid.UUID | None = None
    # Per-fact extras, keyed by _fact_key: evidence (default: the importer's), confidence, extra payload fields.
    evidence: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    confidence: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    extra: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # Evidence for facts without their own (and for create_vendor); default: the importer's.
    default_evidence: list[dict[str, Any]] | None = None
    # Set by automated sources (AI discovery) to record a candidate that was looked at but not proposed.
    discarded: str | None = None
    # Free-form details shown in the report (AI discovery: website, country, highlights, sources).
    details: dict[str, Any] = field(default_factory=dict)
    # Propose replacing HQ country/city that differ from the knowledge base (default: only fill missing ones).
    propose_changes: bool = False

    @property
    def normalized_name(self) -> str:
        return normalize_name(self.name)

    @property
    def dedupe_key(self) -> str:
        return f"d:{self.domain}" if self.domain else f"n:{self.normalized_name}"


@dataclass
class ImportOptions:
    default_avl_status: AvlStatus | None = None
    tags: list[str] = field(default_factory=list)


def parse_row(
    row_number: int, values: list[str], mapping: dict[str, int], tax: Taxonomy, options: ImportOptions
) -> ParsedRow:
    def cell(fld: str) -> str:
        idx = mapping.get(fld)
        return values[idx].strip() if idx is not None and idx < len(values) else ""

    row = ParsedRow(row_number=row_number, name=cell("name")[:300])
    if not row.name:
        row.error = "No vendor name"
        return row
    if not row.normalized_name:
        row.error = f"{row.name!r} isn't a usable vendor name"
        return row

    if website := cell("website"):
        row.website = normalize_website(website)
        row.domain = registrable_domain(row.website)
        if row.website is None or row.domain is None:
            row.website = row.domain = None
            row.messages.append(f"Website {website!r} isn't a usable web address; ignored")
    if country := cell("country"):
        row.country = normalize_country(country)
        if row.country is None:
            row.messages.append(f"Unknown country {country!r}; ignored")
    row.city = cell("city")[:120] or None
    row.description = cell("description")[:5000]
    row.aliases = [a[:300] for a in split_list(cell("aliases")) if normalize_name(a) != row.normalized_name]

    for fld, vocab, target, label in (
        ("equipment", tax.equipment, row.equipment, "equipment category"),
        ("services", tax.services, row.services, "service category"),
    ):
        for term in split_list(cell(fld)):
            if code := vocab.resolve(term):
                target.append((code, term[:300]))
            else:
                row.messages.append(f"Unknown {label} {term!r}; not proposed")
    for fld, vocab, codes, label in (
        ("certifications", tax.certifications, row.certifications, "certification"),
        ("industries", tax.industries, row.industries, "industry"),
    ):
        for term in split_list(cell(fld)):
            if code := vocab.resolve(term):
                if code not in codes:
                    codes.append(code)
            else:
                row.messages.append(f"Unknown {label} {term!r}; not proposed")

    # The tenant's own fields. Only set keys go into the overlay, so blanks never wipe existing values.
    avl_cell = cell("avl_status")
    avl = parse_avl(avl_cell) if avl_cell else options.default_avl_status
    if avl_cell and avl is None:
        row.messages.append(f"Unknown AVL status {avl_cell!r}; left unchanged")
        avl = options.default_avl_status
    if avl is not None:
        row.overlay["avl_status"] = avl.value
    if code := cell("internal_code"):
        row.overlay["internal_vendor_code"] = code[:80]
    tags = [t[:60] for t in [*options.tags, *split_list(cell("tags"))]]
    if tags:
        row.overlay["tags"] = sorted(set(tags))[:30]
    if notes := cell("notes"):
        row.overlay["notes"] = notes[:5000]
    return row


# ---------------------------------------------------------------- the importer


def _fact_key(action: ProposalItemAction, payload: dict[str, Any]) -> tuple[str, str]:
    """What makes two proposed facts "the same" for de-duplication against open proposals."""
    key = {
        ProposalItemAction.ADD_EQUIPMENT: "category_code",
        ProposalItemAction.ADD_SERVICE: "category_code",
        ProposalItemAction.ADD_CERTIFICATION: "certification_code",
        ProposalItemAction.ADD_INDUSTRY: "industry_code",
        ProposalItemAction.ADD_DOMAIN: "domain",
        ProposalItemAction.UPDATE_VENDOR_FIELD: "field",
    }.get(action)
    if action == ProposalItemAction.ADD_ALIAS:
        return action.value, normalize_name(str(payload.get("alias", "")))
    if action == ProposalItemAction.ADD_PROJECT:
        return action.value, normalize_name(str(payload.get("project_name", "")))
    return action.value, str(payload.get(key, "")) if key else ""


@dataclass
class RowResult:
    row_number: int
    outcome: ImportRowOutcome
    vendor_name: str
    vendor_id: uuid.UUID | None = None
    proposal_id: uuid.UUID | None = None
    messages: list[str] = field(default_factory=list)


class VendorListImporter:
    def __init__(
        self,
        session: AsyncSession,
        taxonomy: Taxonomy,
        *,
        tenant_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
        job_id: uuid.UUID | None,
        upload_id: uuid.UUID | None,
        auto_approve: bool,
        options: ImportOptions,
        dry_run: bool = False,
        origin: ProposalOrigin = ProposalOrigin.EXCEL,
        evidence: list[dict[str, Any]] | None = None,
        link_to_tenant: bool = True,
    ) -> None:
        self.origin = origin
        # Imports put vendors on the organization's own list; discovery only finds them.
        self.link_to_tenant = link_to_tenant
        self.evidence = evidence or EVIDENCE  # default evidence for every proposed fact
        self.s = session
        self.tax = taxonomy
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.job_id = job_id
        self.upload_id = upload_id
        self.auto_approve = auto_approve
        self.options = options
        self.dry_run = dry_run
        self.proposals = ProposalService(session, None)
        self._seen: dict[str, int] = {}  # dedupe key -> first row number
        self._open_by_domain: dict[str, uuid.UUID] = {}
        self._open_by_name: dict[str, uuid.UUID] = {}
        self._open_loaded = False

    # ------------------------------------------------------------------ lookups

    async def _load_open_proposals(self) -> None:
        """Open new-vendor proposals (from any tenant), so the same vendor isn't proposed twice."""
        if self._open_loaded:
            return
        rows = await self.s.execute(
            select(KnowledgeProposal.id, ProposalItem.payload)
            .join(ProposalItem, ProposalItem.proposal_id == KnowledgeProposal.id)
            .where(
                KnowledgeProposal.kind == ProposalKind.NEW_VENDOR,
                KnowledgeProposal.status.in_([ProposalStatus.PENDING_REVIEW, ProposalStatus.NEEDS_INFO]),
                ProposalItem.action == ProposalItemAction.CREATE_VENDOR,
            )
        )
        for proposal_id, payload in rows:
            self._remember_proposal(proposal_id, payload.get("legal_name", ""), payload.get("website"))
        self._open_loaded = True

    def _remember_proposal(self, proposal_id: uuid.UUID, name: str, website: str | None) -> None:
        if domain := registrable_domain(website):
            self._open_by_domain.setdefault(domain, proposal_id)
        if normalized := normalize_name(name):
            self._open_by_name.setdefault(normalized, proposal_id)

    async def _by_domain(self, domain: str) -> Vendor | None:
        return await self.s.scalar(
            select(Vendor)
            .join(VendorDomain, VendorDomain.vendor_id == Vendor.id)
            .where(VendorDomain.domain == domain, Vendor.status != VendorStatus.RETIRED)
        )

    async def _by_name(self, normalized: str) -> list[Vendor]:
        alias_ids = select(VendorAlias.vendor_id).where(VendorAlias.normalized_alias == normalized)
        return list(
            await self.s.scalars(
                select(Vendor).where(
                    Vendor.status != VendorStatus.RETIRED,
                    (Vendor.normalized_name == normalized) | Vendor.id.in_(alias_ids),
                )
            )
        )

    async def match(self, row: ParsedRow) -> tuple[Vendor | None, str | None]:
        """(vendor, None) on a match; (None, reason) when ambiguous; (None, None) when the vendor is new."""
        if row.vendor_id:
            chosen = await self.s.get(Vendor, row.vendor_id)
            if chosen is None or chosen.status == VendorStatus.RETIRED:
                return None, "The chosen vendor no longer exists"
            return chosen, None
        if row.domain and (vendor := await self._by_domain(row.domain)):
            return vendor, None
        candidates = await self._by_name(row.normalized_name)
        if row.domain:
            # The row has a website no vendor owns: a same-named vendor with a different website is
            # probably another company (curators still see the name clash when reviewing).
            candidates = [v for v in candidates if not v.domains]
        if len(candidates) > 1 and row.country:
            candidates = [v for v in candidates if v.hq_country in (None, row.country)]
        if len(candidates) == 1:
            return candidates[0], None
        if len(candidates) > 1:
            names = ", ".join(sorted(f"{v.display_name} ({v.hq_country or '?'})" for v in candidates)[:5])
            return None, f"Matches several vendors: {names}. Add a website or country to pick one"
        return await self._fuzzy(row), None

    async def _fuzzy(self, row: ParsedRow) -> Vendor | None:
        """Near-identical names (trigram similarity >= SAME) are the same vendor unless the websites disagree;
        the grey zone is only reported, so a curator (or the AI, in discovery) decides."""
        from app.modules.vendors.dedup import (  # local import: avoids an import cycle
            GREY,
            SAME,
            similar_vendors,
        )

        compatible = [
            sim for sim in await similar_vendors(self.s, row.name, min_similarity=GREY)
            if not (row.domain and sim.vendor.domains and row.domain not in {d.domain for d in sim.vendor.domains})
            and not (row.country and sim.vendor.hq_country and row.country != sim.vendor.hq_country)
        ]  # fmt: skip
        if compatible and compatible[0].similarity >= SAME:
            row.details["matched_by"] = f"similar name ({compatible[0].similarity:.2f})"
            return compatible[0].vendor
        if compatible:
            row.details["possible_duplicates"] = [
                {
                    "vendor_id": str(c.vendor.id),
                    "name": c.vendor.display_name,
                    "similarity": round(c.similarity, 2),
                }
                for c in compatible[:3]
            ]
            best = compatible[0]
            row.messages.append(
                f"Possibly the same company as {best.vendor.display_name} (name similarity {best.similarity:.2f})"
            )
        return None

    # ------------------------------------------------------------------ proposal items

    def _item(self, row: ParsedRow, action: ProposalItemAction, payload: dict[str, Any]) -> NewItem:
        key = _fact_key(action, payload)
        return NewItem(
            action,
            {**payload, **row.extra.get(key, {})},
            row.evidence.get(key, row.default_evidence or self.evidence),
            row.confidence.get(key),
        )

    def _fact_items(self, row: ParsedRow) -> list[NewItem]:
        A = ProposalItemAction  # noqa: N806 - local alias
        items = [self._item(row, A.ADD_ALIAS, {"alias": a}) for a in row.aliases]
        items += [self._item(row, A.ADD_EQUIPMENT, {"category_code": c, "name": n}) for c, n in row.equipment]
        items += [self._item(row, A.ADD_SERVICE, {"category_code": c, "name": n}) for c, n in row.services]
        items += [self._item(row, A.ADD_CERTIFICATION, {"certification_code": c}) for c in row.certifications]
        items += [self._item(row, A.ADD_INDUSTRY, {"industry_code": c}) for c in row.industries]
        items += [self._item(row, A.ADD_PROJECT, p) for p in row.projects]
        return items

    def _new_vendor_items(self, row: ParsedRow) -> list[NewItem]:
        create = {
            "legal_name": row.name,
            "website": row.website,
            "hq_country": row.country,
            "hq_city": row.city,
            "description": row.description,
        }
        evidence = row.default_evidence or self.evidence
        return [NewItem(ProposalItemAction.CREATE_VENDOR, create, evidence), *self._fact_items(row)]

    async def _missing_fact_items(self, vendor: Vendor, row: ParsedRow) -> list[NewItem]:
        """Items for facts in the row that the vendor doesn't have yet."""
        items: list[NewItem] = []
        if row.domain and not vendor.domains:
            items.append(self._item(row, ProposalItemAction.ADD_DOMAIN, {"domain": row.domain}))
        for fld, value in (
            ("hq_country", row.country),
            ("hq_city", row.city),
            ("description", row.description),
        ):
            current = getattr(vendor, fld)
            conflict = row.propose_changes and fld != "description" and current and value
            if conflict and str(value).strip().lower() != str(current).strip().lower():
                row.details.setdefault("conflicts", []).append(
                    {"field": fld, "current": current, "found": value}
                )
                items.append(
                    self._item(row, ProposalItemAction.UPDATE_VENDOR_FIELD, {"field": fld, "value": value})
                )
            elif value and not current:
                items.append(
                    self._item(row, ProposalItemAction.UPDATE_VENDOR_FIELD, {"field": fld, "value": value})
                )

        known_aliases = {a.normalized_alias for a in vendor.aliases} | {vendor.normalized_name}
        aliases = [a for a in row.aliases if normalize_name(a) not in known_aliases]

        async def ids(column: Any, model: Any) -> set[uuid.UUID]:
            stmt = select(column).where(model.vendor_id == vendor.id)
            if hasattr(model, "status"):
                stmt = stmt.where(model.status == FactStatus.APPROVED)
            return set(await self.s.scalars(stmt))

        have_eq = await ids(VendorEquipment.category_id, VendorEquipment) if row.equipment else set()
        have_sv = await ids(VendorService.category_id, VendorService) if row.services else set()
        have_ct = (
            await ids(VendorCertification.certification_type_id, VendorCertification)
            if row.certifications
            else set()
        )
        have_in = await ids(VendorIndustry.industry_id, VendorIndustry) if row.industries else set()
        have_pr = (
            {normalize_name(n) for n in await self.s.scalars(
                select(VendorProject.project_name).where(
                    VendorProject.vendor_id == vendor.id, VendorProject.status == FactStatus.APPROVED
                )
            )}
            if row.projects
            else set()
        )  # fmt: skip
        missing = ParsedRow(
            row_number=row.row_number,
            aliases=aliases,
            equipment=[(c, n) for c, n in row.equipment if self.tax.ids[("equipment", c)] not in have_eq],
            services=[(c, n) for c, n in row.services if self.tax.ids[("services", c)] not in have_sv],
            certifications=[
                c for c in row.certifications if self.tax.ids[("certifications", c)] not in have_ct
            ],
            industries=[c for c in row.industries if self.tax.ids[("industries", c)] not in have_in],
            projects=[
                p for p in row.projects if normalize_name(str(p.get("project_name", ""))) not in have_pr
            ],
            evidence=row.evidence,
            confidence=row.confidence,
            extra=row.extra,
            default_evidence=row.default_evidence,
        )
        items += self._fact_items(missing)
        if not items:
            return items
        # Facts already waiting in an open proposal (e.g. the same file imported twice) aren't proposed again.
        pending = await self.s.scalars(
            select(ProposalItem)
            .join(KnowledgeProposal, KnowledgeProposal.id == ProposalItem.proposal_id)
            .where(
                KnowledgeProposal.subject_vendor_id == vendor.id,
                KnowledgeProposal.status.in_([ProposalStatus.PENDING_REVIEW, ProposalStatus.NEEDS_INFO]),
                ProposalItem.status == ProposalItemStatus.PENDING,
            )
        )
        pending_keys = {_fact_key(i.action, i.payload) for i in pending}
        return [i for i in items if _fact_key(i.action, i.payload) not in pending_keys]

    async def _submit(
        self, kind: ProposalKind, items: list[NewItem], subject_vendor_id: uuid.UUID | None
    ) -> KnowledgeProposal:
        """Submits in a savepoint, so a proposal the applier can't apply fails this row, not the import."""
        async with self.s.begin_nested():
            return await self.proposals.submit(
                kind,
                self.origin,
                items,
                subject_vendor_id=subject_vendor_id,
                origin_tenant_id=self.tenant_id,
                origin_user_id=self.user_id,
                origin_job_id=self.job_id,
                auto_approve=self.auto_approve,
            )

    # ------------------------------------------------------------------ one row

    async def process(self, row: ParsedRow) -> RowResult:
        result = await self._process(row)
        result.messages = [*row.messages, *result.messages]
        if not self.dry_run:
            self.s.add(
                ImportRow(
                    tenant_id=self.tenant_id,
                    job_id=self.job_id,
                    upload_id=self.upload_id,
                    row_number=row.row_number,
                    outcome=result.outcome,
                    vendor_name=row.name[:300],
                    vendor_id=result.vendor_id,
                    proposal_id=result.proposal_id,
                    overlay=row.overlay,
                    messages=result.messages,
                    details=row.details,
                )
            )
        return result

    async def _process(self, row: ParsedRow) -> RowResult:
        name = row.name
        if row.error:
            return RowResult(row.row_number, ImportRowOutcome.INVALID, name, messages=[row.error])
        if row.discarded:
            return RowResult(row.row_number, ImportRowOutcome.DISCARDED, name, messages=[row.discarded])

        first = self._seen.setdefault(row.dedupe_key, row.row_number)
        if first != row.row_number:
            return RowResult(
                row.row_number,
                ImportRowOutcome.DUPLICATE_IN_FILE,
                name,
                messages=[f"Same vendor as row {first}; skipped"],
            )

        vendor, ambiguity = await self.match(row)
        if ambiguity:
            return RowResult(row.row_number, ImportRowOutcome.AMBIGUOUS, name, messages=[ambiguity])

        if vendor is not None:
            result = RowResult(row.row_number, ImportRowOutcome.MATCHED, name, vendor_id=vendor.id)
            items = await self._missing_fact_items(vendor, row)
            if self.dry_run:
                if items:
                    result.messages.append(f"{len(items)} new fact(s) will be proposed")
                return result
            if self.tenant_id and self.link_to_tenant:
                await upsert_overlay(self.s, self.tenant_id, vendor.id, row.overlay)
            if items:
                try:
                    proposal = await self._submit(ProposalKind.UPDATE_VENDOR, items, vendor.id)
                    result.proposal_id = proposal.id
                    result.messages.append(
                        f"{len(items)} new fact(s) "
                        + ("added" if proposal.status == ProposalStatus.AUTO_APPROVED else "sent for review")
                    )
                except AppError as exc:
                    result.messages.append(f"New facts couldn't be proposed: {exc.detail or exc.title}")
            return result

        await self._load_open_proposals()
        open_id = (row.domain and self._open_by_domain.get(row.domain)) or self._open_by_name.get(
            row.normalized_name
        )
        if open_id:
            return RowResult(
                row.row_number,
                ImportRowOutcome.ALREADY_PROPOSED,
                name,
                proposal_id=None if self.dry_run else open_id,
                messages=[
                    "This vendor is already awaiting review"
                    + ("; it joins your list once approved" if self.link_to_tenant else "")
                ],
            )

        if self.dry_run:
            outcome = ImportRowOutcome.CREATED if self.auto_approve else ImportRowOutcome.PROPOSED
            return RowResult(row.row_number, outcome, name)
        try:
            proposal = await self._submit(ProposalKind.NEW_VENDOR, self._new_vendor_items(row), None)
        except AppError as exc:
            return RowResult(
                row.row_number,
                ImportRowOutcome.INVALID,
                name,
                messages=[f"Couldn't propose this vendor: {exc.detail or exc.title}"],
            )
        if proposal.status == ProposalStatus.AUTO_APPROVED and proposal.subject_vendor_id:
            if self.tenant_id and self.link_to_tenant:
                await upsert_overlay(self.s, self.tenant_id, proposal.subject_vendor_id, row.overlay)
            return RowResult(
                row.row_number,
                ImportRowOutcome.CREATED,
                name,
                vendor_id=proposal.subject_vendor_id,
                proposal_id=proposal.id,
            )
        self._remember_proposal(proposal.id, row.name, row.website)
        return RowResult(
            row.row_number,
            ImportRowOutcome.PROPOSED,
            name,
            proposal_id=proposal.id,
            messages=["Sent for review" + ("; joins your list once approved" if self.link_to_tenant else "")],
        )
