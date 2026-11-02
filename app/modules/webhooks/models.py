"""Webhooks (organizations' HTTPS endpoints notified of events) and the shared API rate-limit counters."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import Entity, TenantScoped

EVENTS = ("vendor.updated", "proposal.approved", "job.completed")
DELIVERY_STATUSES = ("pending", "succeeded", "failed")


class WebhookEndpoint(Entity, TenantScoped, Base):
    __tablename__ = "webhook_endpoints"

    url: Mapped[str] = mapped_column(String(500))
    # Used to sign deliveries (HMAC-SHA256); shown to the organization once, when created or rotated.
    secret: Mapped[str] = mapped_column(String(100))
    events: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    description: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)  # consecutive; reset on success
    disabled_reason: Mapped[str | None] = mapped_column(String(200))
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[int | None] = mapped_column(Integer)


class WebhookDelivery(Entity, TenantScoped, Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="status"),
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
    )

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(10), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RateCounter(Base):
    """Fixed-window request counters shared by every API instance (API-key rate limits)."""

    __tablename__ = "api_rate_counters"

    bucket: Mapped[str] = mapped_column(String(200), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
