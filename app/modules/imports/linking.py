"""Adding imported vendors to the tenant's own list (tenant_vendor_links).

Matched and auto-approved vendors are linked during the import. Rows whose vendor still had to be reviewed keep
their overlay on the import row; once a curator approves the proposal, `link_approved_imports` creates the link.
That runs in platform mode (the curator approving it belongs to a different tenant, or none), right after an
approval and on every maintenance pass as a safety net.
"""

import uuid
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.session import open_session
from app.domain.enums import ActorType, AvlStatus, ProposalKind
from app.domain.principal import TenantContext
from app.modules.audit.service import record_audit
from app.modules.imports.models import ImportRow
from app.modules.proposals.models import KnowledgeProposal
from app.modules.tenant_vendors.models import TenantVendorLink

log = get_logger(__name__)


async def upsert_overlay(
    session: AsyncSession, tenant_id: uuid.UUID, vendor_id: uuid.UUID, overlay: dict[str, Any]
) -> TenantVendorLink:
    """Adds the vendor to the tenant's list, or updates the entry. Only keys present in `overlay` are written
    (tags are merged), so re-importing a sparser file never erases what the tenant already recorded."""
    link = await session.scalar(
        select(TenantVendorLink).where(
            TenantVendorLink.tenant_id == tenant_id, TenantVendorLink.vendor_id == vendor_id
        )
    )
    if link is None:
        link = TenantVendorLink(tenant_id=tenant_id, vendor_id=vendor_id, tags=[], notes="", custom_fields={})
        session.add(link)
    if "avl_status" in overlay:
        link.avl_status = AvlStatus(overlay["avl_status"])
    if "internal_vendor_code" in overlay:
        link.internal_vendor_code = overlay["internal_vendor_code"]
    if "tags" in overlay:
        link.tags = sorted(set(link.tags or []) | set(overlay["tags"]))[:30]
    if "notes" in overlay:
        link.notes = overlay["notes"]
    await session.flush()
    return link


async def link_approved_imports(
    sessionmaker: async_sessionmaker[AsyncSession], proposal_ids: list[uuid.UUID] | None = None
) -> int:
    """Links import rows whose new-vendor proposal has since been approved. Returns how many were linked."""
    async with open_session(sessionmaker, TenantContext.platform()) as s:
        stmt = (
            select(ImportRow, KnowledgeProposal.subject_vendor_id)
            .join(KnowledgeProposal, KnowledgeProposal.id == ImportRow.proposal_id)
            .where(
                ImportRow.vendor_id.is_(None),
                ImportRow.proposal_id.is_not(None),
                KnowledgeProposal.kind == ProposalKind.NEW_VENDOR,
                KnowledgeProposal.subject_vendor_id.is_not(None),  # set when create_vendor was applied
            )
            .order_by(ImportRow.created_at)
            .with_for_update(of=ImportRow, skip_locked=True)
        )
        if proposal_ids is not None:
            stmt = stmt.where(ImportRow.proposal_id.in_(proposal_ids))
        linked = 0
        for row, subject_vendor_id in (await s.execute(stmt)).all():
            vendor_id = cast(uuid.UUID, subject_vendor_id)  # non-null: filtered above
            row.vendor_id = vendor_id
            if row.tenant_id is not None:
                await upsert_overlay(s, row.tenant_id, vendor_id, row.overlay)
                record_audit(
                    s,
                    "tenant_vendor.linked_from_import",
                    actor_type=ActorType.SYSTEM,
                    tenant_id=row.tenant_id,
                    entity_type="vendor",
                    entity_id=vendor_id,
                    changes={"import_row_id": str(row.id), "proposal_id": str(row.proposal_id)},
                )
            linked += 1
        await s.commit()
    if linked:
        log.info("imports.linked_approved", rows=linked)
    return linked
