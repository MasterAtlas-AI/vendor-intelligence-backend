import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base
from app.db.mixins import Entity, TenantScoped

REPORT_TYPES = ("vendor_comparison", "market_scan", "avl", "rfq_summary")
REPORT_FORMATS = ("pdf", "xlsx")
REPORT_STATUSES = ("queued", "ready", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Report(Entity, TenantScoped, Base):
    """A generated report file (PDF or Excel), stored in Postgres like uploads. Tenant-private (RLS)."""

    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint(_in("report_type", REPORT_TYPES), name="report_type"),
        CheckConstraint(_in("format", REPORT_FORMATS), name="format"),
        CheckConstraint(_in("status", REPORT_STATUSES), name="status"),
        Index("ix_reports_tenant_created", "tenant_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    report_type: Mapped[str] = mapped_column(String(30))
    format: Mapped[str] = mapped_column(String(5))
    title: Mapped[str] = mapped_column(String(200))
    parameters: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(10), default="queued")
    error: Mapped[str | None] = mapped_column(Text)
    filename: Mapped[str | None] = mapped_column(String(200))
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))
