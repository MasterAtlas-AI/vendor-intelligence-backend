"""Keeps search vectors in step with the knowledge base.

Each vendor becomes a few text chunks: a 'profile' (who they are and everything approved about them, condensed) and one
chunk per equipment and service fact, so a query about one product line can match it directly. A vendor is stale when it
has no profile vector for its current `knowledge_version` (approvals and merges bump it). The embedding job refreshes
stale vendors in batches, re-embedding only chunks whose text changed.

Embedding the whole knowledge base is cheap (text-embedding-3-small: about US$0.01 per 1,000 vendors), so the
maintenance loop simply queues a refresh whenever something is stale.
"""

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.service import AINotAvailable, CallContext
from app.domain.enums import FactStatus, JobStatus, JobType, VendorStatus
from app.modules.jobs.models import Job
from app.modules.search.models import VendorEmbedding
from app.modules.taxonomy.models import CertificationType, EquipmentCategory, Industry, ServiceCategory
from app.modules.vendors.models import (
    Vendor,
    VendorCertification,
    VendorEquipment,
    VendorIndustry,
    VendorProject,
    VendorService,
)
from app.workers.runner import JobContext

VENDORS_PER_BATCH = 50
PROFILE_MAX_CHARS = 6_000


@dataclass(frozen=True)
class Chunk:
    kind: str  # profile | equipment | service
    ref_id: uuid.UUID
    content: str

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


def _join(items: list[str], sep: str = "; ") -> str:
    return sep.join(i for i in items if i)


async def vendor_chunks(s: AsyncSession, vendor_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Chunk]]:
    """Chunks for each (active) vendor, built from approved facts only."""
    vendors = {v.id: v for v in await s.scalars(select(Vendor).where(Vendor.id.in_(vendor_ids)))}
    approved = FactStatus.APPROVED
    equipment: dict[uuid.UUID, list[tuple[VendorEquipment, str]]] = defaultdict(list)
    for eq, cat in await s.execute(
        select(VendorEquipment, EquipmentCategory.name)
        .join(EquipmentCategory, EquipmentCategory.id == VendorEquipment.category_id)
        .where(VendorEquipment.vendor_id.in_(vendor_ids), VendorEquipment.status == approved)
    ):
        equipment[eq.vendor_id].append((eq, cat))
    services: dict[uuid.UUID, list[tuple[VendorService, str]]] = defaultdict(list)
    for sv, cat in await s.execute(
        select(VendorService, ServiceCategory.name)
        .join(ServiceCategory, ServiceCategory.id == VendorService.category_id)
        .where(VendorService.vendor_id.in_(vendor_ids), VendorService.status == approved)
    ):
        services[sv.vendor_id].append((sv, cat))
    certs: dict[uuid.UUID, list[str]] = defaultdict(list)
    for vid, name in await s.execute(
        select(VendorCertification.vendor_id, CertificationType.name)
        .join(CertificationType, CertificationType.id == VendorCertification.certification_type_id)
        .where(VendorCertification.vendor_id.in_(vendor_ids), VendorCertification.status == approved)
    ):
        certs[vid].append(name)
    industries: dict[uuid.UUID, list[str]] = defaultdict(list)
    for vid, name in await s.execute(
        select(VendorIndustry.vendor_id, Industry.name)
        .join(Industry, Industry.id == VendorIndustry.industry_id)
        .where(VendorIndustry.vendor_id.in_(vendor_ids))
    ):
        industries[vid].append(name)
    projects: dict[uuid.UUID, list[str]] = defaultdict(list)
    for p in await s.scalars(
        select(VendorProject).where(VendorProject.vendor_id.in_(vendor_ids), VendorProject.status == approved)
    ):
        projects[p.vendor_id].append(
            _join(
                [p.project_name, p.facility_type or "", p.client or "", p.country or "", str(p.year or "")],
                ", ",
            )
        )

    out: dict[uuid.UUID, list[Chunk]] = {}
    for vid, v in vendors.items():
        if v.status == VendorStatus.RETIRED:
            continue
        name = v.display_name
        eq_lines = [
            _join(
                [cat, eq.name if eq.name != cat else "", eq.model_series or "", _join(eq.standards, ", ")],
                " - ",
            )
            for eq, cat in equipment[vid]
        ]
        sv_lines = [_join([cat, sv.name if sv.name != cat else ""], " - ") for sv, cat in services[vid]]
        profile = "\n".join(
            line
            for line in [
                f"{name}" + (f" ({v.legal_name})" if v.legal_name != name else ""),
                f"Also known as: {_join([a.alias for a in v.aliases], ', ')}" if v.aliases else "",
                f"Headquarters: {_join([v.hq_city or '', v.hq_country or ''], ', ')}" if v.hq_country else "",
                f"Website: {_join([d.domain for d in v.domains], ', ')}" if v.domains else "",
                v.description,
                f"Industries: {_join(industries[vid], ', ')}" if industries[vid] else "",
                f"Equipment: {_join(eq_lines)}" if eq_lines else "",
                f"Services: {_join(sv_lines)}" if sv_lines else "",
                f"Certifications: {_join(certs[vid], ', ')}" if certs[vid] else "",
                f"Projects: {_join(projects[vid])}" if projects[vid] else "",
            ]
            if line
        )[:PROFILE_MAX_CHARS]
        chunks = [Chunk("profile", vid, profile)]
        for eq, cat in equipment[vid]:
            text = f"{name}: {cat}. {eq.name}" + (f", {eq.model_series}" if eq.model_series else "")
            if eq.standards:
                text += f". Standards: {', '.join(eq.standards)}"
            if eq.applications:
                text += f". Applications: {', '.join(eq.applications)}"
            chunks.append(Chunk("equipment", eq.id, text))
        for sv, cat in services[vid]:
            text = f"{name}: {cat} service. {sv.name}" + (
                f". Regions: {', '.join(sv.regions)}" if sv.regions else ""
            )
            chunks.append(Chunk("service", sv.id, text))
        out[vid] = chunks
    return out


def stale_vendors_query(limit: int) -> Any:
    """Active vendors without a profile vector for their current knowledge version, oldest first."""
    current = exists().where(
        and_(
            VendorEmbedding.vendor_id == Vendor.id,
            VendorEmbedding.kind == "profile",
            VendorEmbedding.knowledge_version == Vendor.knowledge_version,
        )
    )
    return (
        select(Vendor.id)
        .where(Vendor.status != VendorStatus.RETIRED, ~current)
        .order_by(Vendor.created_at)
        .limit(limit)
    )


async def index_vendors(s: AsyncSession, ctx: JobContext, vendor_ids: list[uuid.UUID]) -> dict[str, int]:
    """Brings these vendors' vectors up to date. Returns counts (embedded / reused / removed chunks)."""
    assert ctx.ai is not None
    stats = {"embedded": 0, "reused": 0, "removed": 0}
    chunks = await vendor_chunks(s, vendor_ids)
    versions = dict(
        (await s.execute(select(Vendor.id, Vendor.knowledge_version).where(Vendor.id.in_(vendor_ids)))).all()
    )
    existing: dict[tuple[uuid.UUID, str, uuid.UUID], VendorEmbedding] = {
        (e.vendor_id, e.kind, e.ref_id): e
        for e in await s.scalars(select(VendorEmbedding).where(VendorEmbedding.vendor_id.in_(vendor_ids)))
    }
    model = ctx.settings.openai_embedding_model
    todo: list[tuple[uuid.UUID, Chunk]] = []
    keep: set[tuple[uuid.UUID, str, uuid.UUID]] = set()
    for vid, vendor_chunks_ in chunks.items():
        for c in vendor_chunks_:
            key = (vid, c.kind, c.ref_id)
            keep.add(key)
            row = existing.get(key)
            if row is not None and row.content_sha == c.sha and row.model == model:
                row.knowledge_version = versions[vid]
                stats["reused"] += 1
            else:
                todo.append((vid, c))
    if todo:
        vectors = await ctx.ai.embed(
            [c.content for _, c in todo], CallContext(None, ctx.user_id, ctx.job_id), purpose="vendor_index"
        )
        for (vid, c), vector in zip(todo, vectors, strict=True):
            row = existing.get((vid, c.kind, c.ref_id))
            if row is None:
                row = VendorEmbedding(vendor_id=vid, kind=c.kind, ref_id=c.ref_id)
                s.add(row)
            row.content, row.content_sha, row.model = c.content, c.sha, model
            row.embedding, row.knowledge_version = vector, versions[vid]
            stats["embedded"] += 1
    gone = [e.id for key, e in existing.items() if key not in keep]
    if gone:
        await s.execute(delete(VendorEmbedding).where(VendorEmbedding.id.in_(gone)))
        stats["removed"] = len(gone)
    return stats


async def embedding_handler(ctx: JobContext) -> dict[str, Any]:
    """Input: {"vendor_ids": [...]} for specific vendors, otherwise every stale vendor (up to `limit`)."""
    if ctx.ai is None or not ctx.ai.configured:
        raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
    limit = int(ctx.input.get("limit", 20_000))
    async with ctx.session() as s:
        if ids := ctx.input.get("vendor_ids"):
            vendor_ids = [uuid.UUID(str(i)) for i in ids][:limit]
        else:
            vendor_ids = list(await s.scalars(stale_vendors_query(limit)))
    totals = {"vendors": len(vendor_ids), "embedded": 0, "reused": 0, "removed": 0}
    for i in range(0, len(vendor_ids), VENDORS_PER_BATCH):
        async with ctx.session() as s:
            stats = await index_vendors(s, ctx, vendor_ids[i : i + VENDORS_PER_BATCH])
            await s.commit()
        for k, v in stats.items():
            totals[k] += v
        await ctx.progress("embedding", min(99, int(100 * (i + VENDORS_PER_BATCH) / max(len(vendor_ids), 1))))
    return totals


async def queue_refresh_if_stale(s: AsyncSession) -> bool:
    """Queues one embedding job when some vendor is stale and none is queued or running (maintenance loop)."""
    pending = await s.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.type == JobType.EMBEDDING, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
    )
    if pending or await s.scalar(stale_vendors_query(1)) is None:
        return False
    s.add(Job(tenant_id=None, user_id=None, type=JobType.EMBEDDING, input={}))
    return True
