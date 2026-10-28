import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pg_enum
from app.db.mixins import Entity
from app.domain.enums import JobStatus


class Job(Entity, Base):
    """A unit of background work. The table is both the queue and the record of status/progress."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "uq_jobs_tenant_id_idempotency_key",
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where="idempotency_key IS NOT NULL",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_jobs_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_jobs_claimable", "created_at", postgresql_where="status IN ('queued', 'running')"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    type: Mapped[str] = mapped_column(String(50))
    status: Mapped[JobStatus] = mapped_column(pg_enum(JobStatus, "job_status"), default=JobStatus.QUEUED)
    stage: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[int] = mapped_column(SmallInteger, default=0)
    input: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column()
    error: Mapped[dict[str, Any] | None] = mapped_column()
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    queue: Mapped[str] = mapped_column(String(32), default="default")
    # Queue lease (see app.workers.runner): who is running the job and until when the claim is valid.
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    max_attempts: Mapped[int] = mapped_column(SmallInteger, default=3)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal(0))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
