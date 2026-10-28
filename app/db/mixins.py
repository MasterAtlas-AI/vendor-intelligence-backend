import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.core.ids import uuid7


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Authored:
    """Filled automatically from the session's actor (see `db.session._stamp_authorship`).

    Deliberately not foreign keys: audit columns must survive user deletion/anonymisation.
    """

    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


class TenantScoped:
    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805 - declared_attr receives the class
        return mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)


class Entity(UUIDPk, Timestamps, Authored):
    """Standard columns every table carries: id, created_at, updated_at, created_by, updated_by."""
