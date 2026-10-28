"""Imports every model so `Base.metadata` is complete (Alembic, relationship resolution)."""

from app.db.base import Base
from app.modules.api_keys.models import ApiKey
from app.modules.audit.models import AuditLog
from app.modules.auth.models import RefreshToken
from app.modules.jobs.models import Job
from app.modules.tenants.models import Tenant
from app.modules.users.models import Permission, Role, RolePermission, User, UserRole, UserToken

__all__ = [
    "ApiKey",
    "AuditLog",
    "Base",
    "Job",
    "Permission",
    "RefreshToken",
    "Role",
    "RolePermission",
    "Tenant",
    "User",
    "UserRole",
    "UserToken",
]
