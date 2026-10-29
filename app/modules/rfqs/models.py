"""RFQs: requests for quotation sent to a set of vendors, usually from a project's shortlist. Tenant-private (RLS)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import Entity, TenantScoped

RFQ_STATUSES = ("draft", "issued", "evaluating", "awarded", "closed", "cancelled")
RFQ_VENDOR_STATUSES = ("shortlisted", "invited", "quoted", "declined", "awarded", "lost")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Rfq(Entity, TenantScoped, Base):
    __tablename__ = "rfqs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "rfq_number", name="uq_rfqs_tenant_number"),
        CheckConstraint(_in("status", RFQ_STATUSES), name="status"),
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenant_projects.id", ondelete="SET NULL"), index=True
    )
    rfq_number: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    equipment_categories: Mapped[list[str]] = mapped_column(
        ARRAY(String(160)), default=list
    )  # taxonomy codes
    requirements: Mapped[list[dict[str, Any]]] = mapped_column(
        default=list
    )  # [{"text": "...", "mandatory": true}]
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="draft")
    awarded_vendor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)


class RfqVendor(Entity, TenantScoped, Base):
    __tablename__ = "rfq_vendors"
    __table_args__ = (
        UniqueConstraint("rfq_id", "vendor_id", name="uq_rfq_vendors_rfq_vendor"),
        CheckConstraint(_in("status", RFQ_VENDOR_STATUSES), name="status"),
    )

    rfq_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rfqs.id", ondelete="CASCADE"), index=True)
    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(12), default="shortlisted")
    quote_amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    lead_time_weeks: Mapped[int | None] = mapped_column(Integer)
    quote_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str] = mapped_column(Text, default="")
