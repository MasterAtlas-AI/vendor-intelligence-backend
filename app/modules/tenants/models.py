from typing import Any

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pg_enum
from app.db.mixins import Entity
from app.domain.enums import TenantStatus


class Tenant(Entity, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(CITEXT, unique=True)
    status: Mapped[TenantStatus] = mapped_column(
        pg_enum(TenantStatus, "tenant_status"), default=TenantStatus.TRIAL
    )
    plan: Mapped[str] = mapped_column(String(50), default="starter")
    # Uploads from trusted tenants may be auto-promoted into the global knowledge base (design A1).
    trusted_source: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    quotas: Mapped[dict[str, Any]] = mapped_column(default=dict)
