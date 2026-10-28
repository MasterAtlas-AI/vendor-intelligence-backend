import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pg_enum
from app.db.mixins import Entity
from app.domain.enums import JobStatus


class Job(Entity, Base):
    """Source of truth for background work. Redis only carries the message; status lives here."""

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
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    queue: Mapped[str] = mapped_column(String(32), default="default")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal(0))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
