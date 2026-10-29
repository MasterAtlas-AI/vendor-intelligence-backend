"""Second half of merging duplicate vendors: tenant-private rows.

The approval itself (Applier._merge_vendor) moves everything global inside the curator's transaction, but RLS hides
other organizations' rows from that session. This runs in platform mode right after the approval (and on every
maintenance pass, as a safety net) and moves what still points at a merged vendor:

- tenant_vendor_links: an organization's own record moves to the surviving vendor; if it had records on both, they are
  combined (tags merged, notes kept, the more informative approval status wins);
- import_rows: import and discovery reports point at the surviving vendor.
"""

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.session import open_session
from app.domain.enums import AvlStatus, VendorStatus
from app.domain.principal import TenantContext
from app.modules.imports.models import ImportRow
from app.modules.tenant_vendors.models import TenantVendorLink
from app.modules.vendors.models import Vendor

log = get_logger(__name__)


async def _final_target(s: AsyncSession, vendor_id: uuid.UUID) -> uuid.UUID:
    """Follows merge chains (A -> B, B -> C) to the vendor that still exists."""
    seen: set[uuid.UUID] = set()
    current = vendor_id
    while current not in seen:
        seen.add(current)
        nxt = await s.scalar(select(Vendor.merged_into_id).where(Vendor.id == current))
        if nxt is None:
            return current
        current = nxt
    return current


async def finish_merges(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Moves tenant-private rows off merged vendors. Returns how many organization records were moved or combined."""
    moved = 0
    async with open_session(sessionmaker, TenantContext.platform()) as s:
        merged = list(
            await s.scalars(
                select(Vendor.id).where(
                    Vendor.merged_into_id.is_not(None), Vendor.status == VendorStatus.RETIRED
                )
            )
        )
        if not merged:
            return 0
        links = list(
            await s.scalars(
                select(TenantVendorLink).where(TenantVendorLink.vendor_id.in_(merged)).with_for_update()
            )
        )
        for link in links:
            target_id = await _final_target(s, link.vendor_id)
            other = await s.scalar(
                select(TenantVendorLink).where(
                    TenantVendorLink.tenant_id == link.tenant_id, TenantVendorLink.vendor_id == target_id
                )
            )
            if other is None:
                link.vendor_id = target_id
            else:
                other.tags = sorted(set(other.tags or []) | set(link.tags or []))[:30]
                if other.avl_status == AvlStatus.UNASSESSED and link.avl_status != AvlStatus.UNASSESSED:
                    other.avl_status = link.avl_status
                other.internal_vendor_code = other.internal_vendor_code or link.internal_vendor_code
                if link.notes and link.notes not in other.notes:
                    other.notes = f"{other.notes}\n{link.notes}".strip()[:5000]
                await s.delete(link)
            moved += 1
        for dup_id in merged:
            target_id = await _final_target(s, dup_id)
            await s.execute(
                update(ImportRow).where(ImportRow.vendor_id == dup_id).values(vendor_id=target_id)
            )
        await s.commit()
    if moved:
        log.info("vendors.merge_finished", organization_records=moved)
    return moved
