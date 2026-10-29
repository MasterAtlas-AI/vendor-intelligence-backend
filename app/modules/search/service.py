"""Hybrid vendor search.

1. Understand: the query becomes a SearchIntent (local phrase matching, or the AI on request; edited chips skip this).
2. Retrieve three ranked candidate lists:
   a) structured: vendors with approved facts in the requested categories (sub-categories included), certifications
      or industries;
   b) lexical: Postgres full-text over vendor names and descriptions (any of the words);
   c) semantic: cosine similarity between the query and the vendor_embeddings chunks (when vectors exist).
3. Fuse them with Reciprocal Rank Fusion, apply the hard filters (country, AVL status, my vendors).
4. Score: confidence = weighted equipment / standards+certifications / industries / services / semantic / text /
   data-quality components. Components the query doesn't ask about are left out and the weights re-normalised.
5. Explain: per vendor, the matching facts with their sources.
6. Log the search (also the cache for AI parsing).
"""

import hashlib
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.service import AiService, CallContext
from app.core.exceptions import NotFound
from app.domain.enums import FactStatus, VendorStatus
from app.domain.principal import Principal
from app.modules.imports.importer import Taxonomy
from app.modules.search.intent import clean as clean_intent
from app.modules.search.intent import parse_ai, parse_local
from app.modules.search.models import SearchHistory, VendorEmbedding
from app.modules.search.schemas import (
    MatchReason,
    ParseResult,
    ScoreBreakdown,
    SearchFilters,
    SearchHit,
    SearchIntent,
    SearchRequest,
    SearchResponse,
    SourceRef,
    Suggestion,
)
from app.modules.taxonomy.models import CertificationType, EquipmentCategory, Industry, ServiceCategory
from app.modules.tenant_vendors.models import TenantVendorLink
from app.modules.vendors.models import (
    FactSource,
    Vendor,
    VendorCertification,
    VendorEquipment,
    VendorIndustry,
    VendorService,
    VendorSource,
)
from app.modules.vendors.service import VendorFilters, VendorQueryService

WEIGHTS = {
    "equipment": 0.40,
    "standards_certifications": 0.20,
    "industries": 0.15,
    "services": 0.10,
    "semantic": 0.10,
    "text": 0.10,
    "data_quality": 0.05,
}
RRF_K = 60
CANDIDATES = 150
SEMANTIC_CHUNKS = 200
PARSE_CACHE_DAYS = 30
TAXONOMY_TTL_SECONDS = 1800
MIN_CONFIDENCE = 0.10  # weaker matches are noise (e.g. a valve maker for a pump query)

_taxonomy: tuple[float, Taxonomy] | None = None


async def taxonomy(s: AsyncSession) -> Taxonomy:
    """The taxonomy, cached in-process for a few minutes (it changes rarely; a search shouldn't reload it)."""
    global _taxonomy
    if _taxonomy is None or time.monotonic() - _taxonomy[0] > TAXONOMY_TTL_SECONDS:
        _taxonomy = (time.monotonic(), await Taxonomy.load(s))
    return _taxonomy[1]


def query_key(query: str, parser: str) -> str:
    return hashlib.sha256(f"{parser}:{' '.join(query.lower().split())}".encode()).hexdigest()


def _norm_std(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _subtree(column: Any, codes: list[str]) -> Any:
    return or_(*(or_(column == c, column.like(f"{c}.%")) for c in codes))


def _under(code: str, wanted: str) -> bool:
    return code == wanted or code.startswith(wanted + ".")


@dataclass
class Facts:
    """What a candidate vendor has that can match the intent."""

    equipment: list[tuple[uuid.UUID, str, str, str, list[str]]] = field(
        default_factory=list
    )  # id, code, cat, label, stds
    services: list[tuple[uuid.UUID, str, str]] = field(default_factory=list)  # id, code, label
    certifications: list[tuple[str, str]] = field(default_factory=list)  # code, name
    industries: list[tuple[str, str]] = field(default_factory=list)  # code, name


class SearchService:
    def __init__(self, session: AsyncSession, principal: Principal, ai: AiService) -> None:
        self.s = session
        self.principal = principal
        self.ai = ai

    @property
    def _call(self) -> CallContext:
        return CallContext(self.principal.tenant_id, self.principal.user_id)

    # ------------------------------------------------------------------ understanding

    async def parse(self, query: str, parser: str) -> ParseResult:
        tax = await taxonomy(self.s)
        if parser == "local" or not self.ai.configured:
            return ParseResult(intent=parse_local(query, tax), parser="local")
        key = query_key(query, "ai")
        cached = await self.s.scalar(
            select(SearchHistory.intent)
            .where(
                SearchHistory.query_key == key,
                SearchHistory.created_at > datetime.now(UTC) - timedelta(days=PARSE_CACHE_DAYS),
            )
            .order_by(SearchHistory.created_at.desc())
            .limit(1)
        )
        if cached is not None:
            return ParseResult(
                intent=clean_intent(SearchIntent.model_validate(cached), tax), parser="ai", cached=True
            )
        return ParseResult(intent=await parse_ai(query, tax, self.ai, self._call), parser="ai")

    # ------------------------------------------------------------------ retrieval

    async def _structured(self, intent: SearchIntent, limit: int) -> list[uuid.UUID]:
        parts: list[Select[Any]] = []
        approved_eq = VendorEquipment.status == FactStatus.APPROVED
        if intent.equipment_categories:
            parts.append(
                select(VendorEquipment.vendor_id.label("vid"))
                .join(EquipmentCategory, EquipmentCategory.id == VendorEquipment.category_id)
                .where(approved_eq, _subtree(EquipmentCategory.code, intent.equipment_categories))
            )
        if intent.service_categories:
            parts.append(
                select(VendorService.vendor_id.label("vid"))
                .join(ServiceCategory, ServiceCategory.id == VendorService.category_id)
                .where(
                    VendorService.status == FactStatus.APPROVED,
                    _subtree(ServiceCategory.code, intent.service_categories),
                )
            )
        if intent.certifications:
            parts.append(
                select(VendorCertification.vendor_id.label("vid"))
                .join(CertificationType, CertificationType.id == VendorCertification.certification_type_id)
                .where(
                    VendorCertification.status == FactStatus.APPROVED,
                    CertificationType.code.in_(intent.certifications),
                )
            )
        if intent.industries:
            parts.append(
                select(VendorIndustry.vendor_id.label("vid"))
                .join(Industry, Industry.id == VendorIndustry.industry_id)
                .where(Industry.code.in_(intent.industries))
            )
        if not parts:
            return []
        hits = parts[0].union_all(*parts[1:]).subquery()
        rows = await self.s.execute(
            select(hits.c.vid, func.count().label("n"))
            .group_by(hits.c.vid)
            .order_by(literal_column("n").desc())
            .limit(limit)
        )
        return [r[0] for r in rows]

    async def _lexical(self, text: str, limit: int) -> list[uuid.UUID]:
        words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1][:20]
        if not words:
            return []
        ts = func.to_tsquery("simple", " | ".join(words))
        rows = await self.s.execute(
            select(Vendor.id)
            .where(Vendor.status == VendorStatus.ACTIVE, Vendor.search_tsv.op("@@")(ts))
            .order_by(func.ts_rank(Vendor.search_tsv, ts).desc())
            .limit(limit)
        )
        return [r[0] for r in rows]

    async def _semantic(self, text: str) -> tuple[list[uuid.UUID], dict[uuid.UUID, tuple[float, str]]] | None:
        """Vendors by best chunk similarity, with (similarity, chunk text). None when vectors aren't available."""
        if not text.strip() or not self.ai.configured:
            return None
        if await self.s.scalar(select(VendorEmbedding.id).limit(1)) is None:
            return None
        [vector] = await self.ai.embed([text], self._call, purpose="search_query")
        distance = VendorEmbedding.embedding.cosine_distance(vector)
        nearest = (
            select(VendorEmbedding.vendor_id, VendorEmbedding.content, distance.label("d"))
            .order_by(distance)
            .limit(SEMANTIC_CHUNKS)
            .subquery()
        )
        best: dict[uuid.UUID, tuple[float, str]] = {}
        for vid, content, d in await self.s.execute(select(nearest).order_by(nearest.c.d)):
            if vid not in best:
                best[vid] = (max(0.0, 1.0 - float(d)), content)
        return list(best), best

    # ------------------------------------------------------------------ scoring

    async def _facts(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, Facts]:
        facts: dict[uuid.UUID, Facts] = defaultdict(Facts)
        for eq, code, name in await self.s.execute(
            select(VendorEquipment, EquipmentCategory.code, EquipmentCategory.name)
            .join(EquipmentCategory, EquipmentCategory.id == VendorEquipment.category_id)
            .where(VendorEquipment.vendor_id.in_(ids), VendorEquipment.status == FactStatus.APPROVED)
        ):
            label = name if eq.name == name else f"{name}: {eq.name}"
            if eq.model_series and eq.model_series not in label:
                label += f" ({eq.model_series})"
            facts[eq.vendor_id].equipment.append((eq.id, code, name, label, list(eq.standards)))
        for sv, code, name in await self.s.execute(
            select(VendorService, ServiceCategory.code, ServiceCategory.name)
            .join(ServiceCategory, ServiceCategory.id == VendorService.category_id)
            .where(VendorService.vendor_id.in_(ids), VendorService.status == FactStatus.APPROVED)
        ):
            facts[sv.vendor_id].services.append(
                (sv.id, code, name if sv.name == name else f"{name}: {sv.name}")
            )
        for vid, code, name in await self.s.execute(
            select(VendorCertification.vendor_id, CertificationType.code, CertificationType.name)
            .join(CertificationType, CertificationType.id == VendorCertification.certification_type_id)
            .where(VendorCertification.vendor_id.in_(ids), VendorCertification.status == FactStatus.APPROVED)
        ):
            facts[vid].certifications.append((code, name))
        for vid, code, name in await self.s.execute(
            select(VendorIndustry.vendor_id, Industry.code, Industry.name)
            .join(Industry, Industry.id == VendorIndustry.industry_id)
            .where(VendorIndustry.vendor_id.in_(ids))
        ):
            facts[vid].industries.append((code, name))
        return facts

    async def _sources(self, table: str, fact_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[SourceRef]]:
        out: dict[uuid.UUID, list[SourceRef]] = defaultdict(list)
        if not fact_ids:
            return out
        for fid, snippet, url, title in await self.s.execute(
            select(FactSource.fact_id, FactSource.snippet, VendorSource.url, VendorSource.title)
            .join(VendorSource, VendorSource.id == FactSource.source_id)
            .where(FactSource.fact_table == table, FactSource.fact_id.in_(fact_ids))
        ):
            if len(out[fid]) < 2:
                out[fid].append(SourceRef(url=url, title=title or "", snippet=(snippet or "")[:300]))
        return out

    def _score(
        self,
        intent: SearchIntent,
        f: Facts,
        vendor: Vendor,
        semantic: dict[uuid.UUID, tuple[float, str]] | None,
        lexical_rank: dict[uuid.UUID, int],
        has_text: bool,
        cert_names: dict[str, str],
    ) -> tuple[float, ScoreBreakdown, dict[str, list[Any]]]:
        matched: dict[str, list[Any]] = defaultdict(list)
        scores = ScoreBreakdown(data_quality=0.0)
        if intent.equipment_categories:
            per = []
            for wanted in intent.equipment_categories:
                exact = [e for e in f.equipment if _under(e[1], wanted)]
                broader = [e for e in f.equipment if wanted.startswith(e[1] + ".")]
                matched["equipment"] += exact or broader
                per.append(1.0 if exact else 0.5 if broader else 0.0)
            scores.equipment = sum(per) / len(per)
        if intent.standards or intent.certifications:
            have_std = {_norm_std(s) for e in f.equipment for s in e[4]} | {
                _norm_std(n) for _, n in f.certifications
            }
            have_std |= {_norm_std(c) for c, _ in f.certifications}
            hits = 0
            for std in intent.standards:
                if any(_norm_std(std) in h for h in have_std):
                    hits += 1
                    matched["standard"].append(std)
            # A certification that names a requested standard ("API 610" and the API_610 certification type) is the
            # same requirement: met by either the certificate or equipment built to that standard.
            wanted_std = [_norm_std(x) for x in intent.standards]
            cert_codes = {c for c, _ in f.certifications}
            extra = 0
            for code in intent.certifications:
                if code in cert_codes:
                    matched["certification"] += [n for c, n in f.certifications if c == code]
                if any(
                    w and (w in _norm_std(code) or w in _norm_std(cert_names.get(code, "")))
                    for w in wanted_std
                ):
                    continue
                extra += 1
                hits += code in cert_codes
            scores.standards_certifications = hits / (len(intent.standards) + extra)
        if intent.industries:
            have = {c: n for c, n in f.industries}
            matched["industry"] = [have[c] for c in intent.industries if c in have]
            scores.industries = len(matched["industry"]) / len(intent.industries)
        if intent.service_categories:
            per = []
            for wanted in intent.service_categories:
                hit = [sv for sv in f.services if _under(sv[1], wanted)]
                matched["service"] += hit
                per.append(1.0 if hit else 0.0)
            scores.services = sum(per) / len(per)
        if semantic is not None:
            sim, chunk = semantic.get(vendor.id, (0.0, ""))
            scores.semantic = round(sim, 3)
            if chunk:
                matched["semantic"].append(chunk)
        if has_text:
            rank = lexical_rank.get(vendor.id)
            scores.text = 0.0 if rank is None else round(1.0 / (1 + rank / 10), 3)
        facts_n = len(f.equipment) + len(f.services) + len(f.certifications)
        scores.data_quality = round(
            float(vendor.data_quality_score)
            if vendor.data_quality_score is not None
            else 0.5 * bool(vendor.website) + 0.5 * min(1.0, facts_n / 5),
            3,
        )
        parts = {k: v for k, v in scores.model_dump().items() if v is not None}
        total = sum(WEIGHTS[k] for k in parts)
        confidence = sum(WEIGHTS[k] * v for k, v in parts.items()) / total if total else 0.0
        return round(confidence, 3), scores, matched

    # ------------------------------------------------------------------ search

    async def search(self, req: SearchRequest) -> SearchResponse:
        started = time.monotonic()
        tax = await taxonomy(self.s)
        parser = req.parser
        if req.intent is not None:
            intent = clean_intent(req.intent, tax)
        elif req.query.strip():
            parsed = await self.parse(req.query, req.parser)
            intent, parser = parsed.intent, parsed.parser
        else:
            intent = SearchIntent()
        filters = req.filters.model_copy(
            update={"countries": list(dict.fromkeys(req.filters.countries + intent.countries))}
        )
        text = intent.keywords or ("" if intent.structured else req.query)
        semantic_text = req.query.strip() or self._describe(intent, tax)
        hard = bool(filters.countries or filters.avl_status or filters.my_vendors_only)
        limit = CANDIDATES * (4 if hard else 1)

        structured = await self._structured(intent, limit)
        lexical = await self._lexical(text, limit)
        sem = await self._semantic(semantic_text)
        lists = [structured, lexical] + ([sem[0]] if sem else [])
        rrf: dict[uuid.UUID, float] = defaultdict(float)
        for ranked in lists:
            for rank, vid in enumerate(ranked):
                rrf[vid] += 1.0 / (RRF_K + rank + 1)
        candidates = sorted(rrf, key=lambda v: -rrf[v])[:limit]
        vendors = await self._filtered_vendors(candidates, filters)

        facts = await self._facts([v.id for v in vendors])
        lexical_rank = {vid: i for i, vid in enumerate(lexical)}
        cert_names = {code: name for (kind, code), name in tax.names.items() if kind == "certifications"}
        scored = []
        for v in vendors:
            conf, scores, matched = self._score(
                intent, facts[v.id], v, sem[1] if sem else None, lexical_rank, bool(text), cert_names
            )
            scored.append((conf, rrf[v.id], v, scores, matched))
        scored = [x for x in scored if x[0] >= MIN_CONFIDENCE]
        scored.sort(key=lambda x: (-x[0], -x[1]))
        top = scored[: req.limit]

        summaries = await self._summaries([t[2].id for t in top])
        eq_sources = await self._sources(
            "vendor_equipment", [e[0] for t in top for e in t[4].get("equipment", [])]
        )
        sv_sources = await self._sources(
            "vendor_services", [sv[0] for t in top for sv in t[4].get("service", [])]
        )
        hits = [
            SearchHit(
                vendor=summaries[v.id],
                confidence=conf,
                scores=scores,
                reasons=self._reasons(matched, eq_sources, sv_sources),
            )
            for conf, _, v, scores, matched in top
            if v.id in summaries
        ]
        took = int((time.monotonic() - started) * 1000)
        search_id = await self._log(req, intent, parser, filters, hits, took)
        return SearchResponse(search_id=search_id, intent=intent, parser=parser, semantic=sem is not None,
                              took_ms=took, results=hits)  # fmt: skip

    @staticmethod
    def _describe(intent: SearchIntent, tax: Taxonomy) -> str:
        names = [tax.names.get((k, c), c) for k, field_ in (("equipment", "equipment_categories"),
                 ("services", "service_categories"), ("industries", "industries"),
                 ("certifications", "certifications")) for c in getattr(intent, field_)]  # fmt: skip
        return " ".join([*names, *intent.standards, intent.keywords]).strip()

    @staticmethod
    def _reasons(
        matched: dict[str, list[Any]],
        eq_sources: dict[uuid.UUID, list[SourceRef]],
        sv_sources: dict[uuid.UUID, list[SourceRef]],
    ) -> list[MatchReason]:
        reasons: list[MatchReason] = []
        seen: set[uuid.UUID] = set()
        for fid, code, _, label, stds in matched.get("equipment", []):
            if fid not in seen and len(seen) < 5:
                seen.add(fid)
                text = label + (f" · {', '.join(stds)}" if stds else "")
                reasons.append(
                    MatchReason(kind="equipment", label=text, code=code, sources=eq_sources.get(fid, []))
                )
        for fid, code, label in matched.get("service", [])[:3]:
            reasons.append(
                MatchReason(kind="service", label=label, code=code, sources=sv_sources.get(fid, []))
            )
        for name in dict.fromkeys(matched.get("certification", [])):
            reasons.append(MatchReason(kind="certification", label=name))
        for std in matched.get("standard", []):
            reasons.append(MatchReason(kind="standard", label=std))
        for name in matched.get("industry", []):
            reasons.append(MatchReason(kind="industry", label=name))
        if not reasons and matched.get("semantic"):
            chunk = " ".join(matched["semantic"][0].split())
            reasons.append(
                MatchReason(kind="semantic", label=chunk[:200] + ("…" if len(chunk) > 200 else ""))
            )
        return reasons

    async def _filtered_vendors(self, ids: list[uuid.UUID], filters: SearchFilters) -> list[Vendor]:
        if not ids:
            return []
        stmt = select(Vendor).where(Vendor.id.in_(ids), Vendor.status == VendorStatus.ACTIVE)
        if filters.countries:
            stmt = stmt.where(Vendor.hq_country.in_(filters.countries))
        if (filters.my_vendors_only or filters.avl_status) and self.principal.tenant_id is not None:
            link = and_(
                TenantVendorLink.vendor_id == Vendor.id,
                TenantVendorLink.tenant_id == self.principal.tenant_id,
            )
            stmt = stmt.join(TenantVendorLink, link)
            if filters.avl_status:
                stmt = stmt.where(TenantVendorLink.avl_status.in_(filters.avl_status))
        return list(await self.s.scalars(stmt))

    async def _summaries(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
        if not ids:
            return {}
        svc = VendorQueryService(self.s, self.principal)
        items, _ = await svc.search(VendorFilters(vendor_ids=tuple(ids)), limit=len(ids), cursor=None)
        return {i.id: i for i in items}

    async def _log(
        self, req: SearchRequest, intent: SearchIntent, parser: str, filters: SearchFilters, hits: list[SearchHit],
        took: int,
    ) -> uuid.UUID | None:  # fmt: skip
        if self.principal.user_id is None:  # API keys: no personal history
            return None
        row = SearchHistory(
            tenant_id=self.principal.tenant_id, user_id=self.principal.user_id, query=req.query.strip()[:500],
            query_key=query_key(req.query, parser), parser=parser, intent=intent.model_dump(),
            filters=filters.model_dump(mode="json"), result_ids=[h.vendor.id for h in hits],
            result_count=len(hits), took_ms=took,
        )  # fmt: skip
        self.s.add(row)
        await self.s.commit()
        return row.id

    # ------------------------------------------------------------------ history and suggestions

    async def history(self, limit: int) -> list[SearchHistory]:
        return list(
            await self.s.scalars(
                select(SearchHistory)
                .where(SearchHistory.user_id == self.principal.user_id, SearchHistory.query != "")
                .order_by(SearchHistory.created_at.desc())
                .limit(limit)
            )
        )

    async def delete_history(self, entry_id: uuid.UUID) -> None:
        row = await self.s.get(SearchHistory, entry_id)
        if row is None or row.user_id != self.principal.user_id:
            raise NotFound("Search not found")
        await self.s.delete(row)
        await self.s.commit()

    async def suggest(self, q: str, limit: int = 8) -> list[Suggestion]:
        q = q.strip()
        if len(q) < 2:
            return []
        like = f"%{q.lower()}%"
        out: list[Suggestion] = []
        for model, kind in ((EquipmentCategory, "equipment"), (ServiceCategory, "service"),
                            (CertificationType, "certification"), (Industry, "industry")):  # fmt: skip
            for code, name in await self.s.execute(
                select(model.code, model.name)
                .where(func.lower(model.name).like(like))
                .order_by(model.name)
                .limit(3)
            ):
                out.append(Suggestion(kind=kind, label=name, value=code))
        for vid, name in await self.s.execute(
            select(Vendor.id, Vendor.display_name)
            .where(Vendor.status == VendorStatus.ACTIVE, func.lower(Vendor.display_name).like(like))
            .order_by(func.similarity(Vendor.normalized_name, q.lower()).desc())
            .limit(limit)
        ):
            out.append(Suggestion(kind="vendor", label=name, value=str(vid)))
        return out[: limit * 2]
