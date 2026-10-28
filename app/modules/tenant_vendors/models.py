import uuid
from typing import Any

from sqlalchemy import ARRAY, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pg_enum
from app.db.mixins import Entity, TenantScoped
from app.domain.enums import AvlStatus


class TenantVendorLink(Entity, TenantScoped, Base):
    """A tenant's private view of a global vendor: its own approval (AVL) status, internal code, tags and notes.

    This is how tenants "own" vendor relationships without copying the vendor. RLS keeps it tenant-private.
    """

    __tablename__ = "tenant_vendor_links"
    __table_args__ = (UniqueConstraint("tenant_id", "vendor_id"),)

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    avl_status: Mapped[AvlStatus] = mapped_column(
        pg_enum(AvlStatus, "avl_status"), default=AvlStatus.UNASSESSED
    )
    internal_vendor_code: Mapped[str | None] = mapped_column(String(80))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    custom_fields: Mapped[dict[str, Any]] = mapped_column(default=dict)
