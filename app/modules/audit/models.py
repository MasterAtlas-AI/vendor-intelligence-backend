import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import uuid7
from app.db.base import Base, pg_enum
from app.domain.enums import ActorType


class AuditLog(Base):
    """Append-only (the app role has no UPDATE/DELETE grant) and range-partitioned by month on created_at,
    hence the composite primary key and no updated_* columns. Created by raw SQL in migration 0002."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # Set client-side (not only server_default) so INSERTs need no RETURNING: with RLS, RETURNING also applies
    # the SELECT policy to the new row, which would block curators writing platform-level (tenant_id NULL) events.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType, "actor_type"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column()
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    changes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))
