"""Duplicate detection (design §6.4): exact keys, then fuzzy names, then AI judgement for the grey zone.

score = 1.0 for a shared website domain or an identical normalised name/alias, otherwise pg_trgm similarity of the
normalised names (aliases included):
    >= SAME  -> the same vendor
    >= GREY  -> maybe: the AI decides when available, else curators are told ("possible duplicate")
    <  GREY  -> a different vendor
(The design's embedding similarity joins in Phase 4; trigram similarity alone is already a strong signal for names.)

`dedup_scan_handler` sweeps the whole knowledge base and raises merge proposals for curators.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.ports import AIError
from app.ai.service import AiService, BudgetExceeded, CallContext
from app.core.logging import get_logger
from app.discovery.prompts import DEDUP_JUDGE
from app.discovery.schemas import SameCompany
from app.domain.enums import ProposalItemAction, ProposalKind, ProposalOrigin, VendorStatus
from app.domain.normalization import normalize_name
from app.modules.proposals.models import KnowledgeProposal, ProposalItem
from app.modules.proposals.service import NewItem, ProposalService
from app.modules.vendors.models import (
    Vendor,
    VendorAlias,
    VendorCertification,
    VendorEquipment,
    VendorService,
)
from app.workers.runner import JobContext

log = get_logger(__name__)

SAME = 0.92
GREY = 0.80
JUDGE_MIN_CONFIDENCE = 0.7


@dataclass
class Similar:
    vendor: Vendor
    similarity: float


async def similar_vendors(
    s: AsyncSession, name: str, *, min_similarity: float = GREY, limit: int = 5
) -> list[Similar]:
    """Active vendors whose normalised name or an alias is similar to `name` (pg_trgm), most similar first."""
    key = normalize_name(name)
    if not key:
        return []
    by_name = select(Vendor.id, func.similarity(Vendor.normalized_name, key).label("sim")).where(
        Vendor.status != VendorStatus.RETIRED, Vendor.normalized_name.op("%")(key)
    )
    by_alias = (
        select(VendorAlias.vendor_id, func.similarity(VendorAlias.normalized_alias, key).label("sim"))
        .join(Vendor, Vendor.id == VendorAlias.vendor_id)
        .where(Vendor.status != VendorStatus.RETIRED, VendorAlias.normalized_alias.op("%")(key))
    )
    best: dict[uuid.UUID, float] = {}
    for vendor_id, sim in [*(await s.execute(by_name)).all(), *(await s.execute(by_alias)).all()]:
        best[vendor_id] = max(best.get(vendor_id, 0.0), float(sim))
    ranked = sorted(((v, sim) for v, sim in best.items() if sim >= min_similarity), key=lambda x: -x[1])[
        :limit
    ]
    out = []
    for vendor_id, sim in ranked:
        vendor = await s.get(Vendor, vendor_id)
        if vendor is not None:
            out.append(Similar(vendor, sim))
    return out


async def profile(s: AsyncSession, v: Vendor) -> dict[str, Any]:
    """What the AI judge sees about a record (no tenant data)."""

    async def names(model: Any) -> list[str]:
        return list(await s.scalars(select(model.name).where(model.vendor_id == v.id).limit(8)))

    certs = await s.scalar(
        select(func.count()).select_from(VendorCertification).where(VendorCertification.vendor_id == v.id)
    )
    return {
        "name": v.display_name, "legal_name": v.legal_name, "aliases": [a.alias for a in v.aliases][:10],
        "websites": [d.domain for d in v.domains], "country": v.hq_country, "city": v.hq_city,
        "description": v.description[:600], "equipment": await names(VendorEquipment),
        "services": await names(VendorService), "certifications": certs,
    }  # fmt: skip


async def judge(ai: AiService, call: CallContext, a: dict[str, Any], b: dict[str, Any]) -> SameCompany:
    return (await ai.run(DEDUP_JUDGE, SameCompany, call, a=a, b=b)).output


def domains_conflict(a: Vendor, b: Vendor) -> bool:
    da, db = {d.domain for d in a.domains}, {d.domain for d in b.domains}
    return bool(da and db and not da & db)


# ---------------------------------------------------------------- the scan job

PAIRS_SQL = text(
    """
    SELECT a.id, b.id, similarity(a.normalized_name, b.normalized_name) AS sim
      FROM vendors a
      JOIN vendors b ON a.id < b.id AND a.normalized_name % b.normalized_name
     WHERE a.status <> 'retired' AND b.status <> 'retired'
       AND similarity(a.normalized_name, b.normalized_name) >= :min_sim
    UNION
    SELECT LEAST(v.id, al.vendor_id), GREATEST(v.id, al.vendor_id), 1.0
      FROM vendor_aliases al
      JOIN vendors v ON v.normalized_name = al.normalized_alias AND v.id <> al.vendor_id
      JOIN vendors o ON o.id = al.vendor_id
     WHERE v.status <> 'retired' AND o.status <> 'retired'
     ORDER BY 3 DESC
     LIMIT :max_pairs
    """
)


async def _known_pairs(s: AsyncSession) -> set[frozenset[uuid.UUID]]:
    """Pairs already proposed for merging (open, decided or rejected): never proposed twice."""
    rows = await s.execute(
        select(KnowledgeProposal.subject_vendor_id, ProposalItem.payload)
        .join(ProposalItem, ProposalItem.proposal_id == KnowledgeProposal.id)
        .where(ProposalItem.action == ProposalItemAction.MERGE_VENDOR)
    )
    return {frozenset({sid, uuid.UUID(str(p["duplicate_vendor_id"]))}) for sid, p in rows.all() if sid}


async def _weight(s: AsyncSession, v: Vendor) -> tuple[int, int, float]:
    """Which record survives a merge: more facts, then having a website, then the older one."""
    facts = 0
    for model in (VendorEquipment, VendorService, VendorCertification):
        facts += await s.scalar(select(func.count()).select_from(model).where(model.vendor_id == v.id)) or 0
    return facts, 1 if v.domains or v.website else 0, -v.created_at.timestamp()


async def dedup_scan_handler(ctx: JobContext) -> dict[str, Any]:
    min_sim = float(ctx.input.get("min_similarity", 0.6))
    max_pairs = int(ctx.input.get("max_pairs", 200))
    ai = ctx.ai if ctx.ai is not None and ctx.ai.configured and ctx.input.get("use_ai", True) else None
    call = CallContext(ctx.tenant_id, ctx.user_id, ctx.job_id)
    stats = {
        "pairs": 0,
        "proposed": 0,
        "already_known": 0,
        "not_duplicates": 0,
        "needs_ai": 0,
        "ai_checked": 0,
    }

    await ctx.progress("finding pairs", 5)
    async with ctx.session() as s:
        pairs = (await s.execute(PAIRS_SQL, {"min_sim": min_sim, "max_pairs": max_pairs})).all()
        known = await _known_pairs(s)
    stats["pairs"] = len(pairs)

    proposals: list[tuple[uuid.UUID, uuid.UUID, float, str, Decimal]] = []
    for n, (a_id, b_id, sim) in enumerate(pairs, start=1):
        sim = float(sim)
        if frozenset({a_id, b_id}) in known:
            stats["already_known"] += 1
            continue
        async with ctx.session() as s:
            a, b = await s.get(Vendor, a_id), await s.get(Vendor, b_id)
            if a is None or b is None:
                continue
            conflict = domains_conflict(a, b)
            reason, confidence = "", Decimal(str(round(sim, 2)))
            if sim >= SAME and not conflict:
                reason = f"Names are nearly identical (similarity {sim:.2f})"
            elif ai is not None:
                try:
                    verdict = await judge(ai, call, await profile(s, a), await profile(s, b))
                except BudgetExceeded:
                    ai = None
                    stats["needs_ai"] += 1
                    continue
                except AIError as exc:
                    log.warning("dedup.judge_failed", error=str(exc)[:200])
                    continue
                stats["ai_checked"] += 1
                if verdict.relationship != "same" or verdict.confidence < JUDGE_MIN_CONFIDENCE:
                    stats["not_duplicates"] += 1
                    continue
                reason = f"AI: {verdict.reason[:300]} (name similarity {sim:.2f})"
                confidence = Decimal(str(round(min(verdict.confidence, 0.99), 2)))
            elif sim >= GREY and not conflict:
                reason = f"Similar names (similarity {sim:.2f}); no AI check was available, please verify"
            else:
                stats["needs_ai" if not conflict or sim >= GREY else "not_duplicates"] += 1
                continue
            keep, drop = (a, b) if await _weight(s, a) >= await _weight(s, b) else (b, a)
            proposals.append((keep.id, drop.id, sim, reason, confidence))
            known.add(frozenset({a_id, b_id}))
        if n % 10 == 0:
            await ctx.progress("checking pairs", 5 + int(85 * n / max(len(pairs), 1)))

    await ctx.progress("proposing", 92)
    async with ctx.session() as s:
        svc = ProposalService(s, None)
        for keep_id, drop_id, sim, reason, confidence in proposals:
            kept, dropped = await s.get(Vendor, keep_id), await s.get(Vendor, drop_id)
            if kept is None or dropped is None:
                continue
            await svc.submit(
                ProposalKind.MERGE_VENDORS,
                ProposalOrigin.DEDUP_SCAN,
                [NewItem(ProposalItemAction.MERGE_VENDOR,
                         {"duplicate_vendor_id": str(drop_id), "similarity": round(sim, 3), "reason": reason})],
                title=f"Merge {dropped.display_name} into {kept.display_name}",
                subject_vendor_id=keep_id, origin_job_id=ctx.job_id, origin_user_id=ctx.user_id,
                confidence=confidence,
            )  # fmt: skip
            stats["proposed"] += 1
        await s.commit()
    return stats
