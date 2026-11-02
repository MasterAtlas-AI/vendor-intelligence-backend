from datetime import datetime

from sqlalchemy import ARRAY, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import CIDR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import Entity, TenantScoped


class ApiKey(Entity, TenantScoped, Base):
    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    ip_allowlist: Mapped[list[str]] = mapped_column(ARRAY(CIDR), default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=120, server_default="120")
