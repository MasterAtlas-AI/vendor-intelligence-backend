import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, pg_enum
from app.db.mixins import Entity
from app.domain.enums import UserStatus, UserTokenPurpose


class Permission(Entity, Base):
    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")


class Role(Entity, Base):
    """System roles have tenant_id NULL and are visible to every tenant; custom roles belong to one tenant."""

    __tablename__ = "roles"
    __table_args__ = (
        Index("uq_roles_tenant_id_key", "tenant_id", "key", unique=True, postgresql_nulls_not_distinct=True),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)

    permissions: Mapped[list[Permission]] = relationship(
        secondary="role_permissions", lazy="selectin", viewonly=True
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class User(Entity, Base):
    """tenant_id is NULL only for platform staff (super admins, curators)."""

    __tablename__ = "users"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[UserStatus] = mapped_column(pg_enum(UserStatus, "user_status"), default=UserStatus.INVITED)
    platform_roles: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bumped whenever roles change, so clients can refresh cached permissions.
    perms_version: Mapped[int] = mapped_column(Integer, default=1)

    roles: Mapped[list[Role]] = relationship(secondary="user_roles", lazy="selectin", viewonly=True)

    @property
    def role_keys(self) -> list[str]:
        return sorted(r.key for r in self.roles)

    @property
    def tenant_permissions(self) -> set[str]:
        return {p.key for r in self.roles for p in r.permissions}


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)


class UserToken(Entity, Base):
    """Single-use tokens for invitations, password resets and email verification (stored hashed)."""

    __tablename__ = "user_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    purpose: Mapped[UserTokenPurpose] = mapped_column(pg_enum(UserTokenPurpose, "user_token_purpose"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
