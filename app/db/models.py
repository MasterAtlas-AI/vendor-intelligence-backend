"""Imports every model so `Base.metadata` is complete (Alembic, relationship resolution)."""

from app.db.base import Base
from app.modules.api_keys.models import ApiKey
from app.modules.audit.models import AuditLog
from app.modules.auth.models import RefreshToken
from app.modules.imports.models import ImportRow, Upload
from app.modules.jobs.models import Job
from app.modules.proposals.models import ApprovalHistory, KnowledgeProposal, ProposalItem
from app.modules.taxonomy.models import CertificationType, EquipmentCategory, Industry, ServiceCategory
from app.modules.tenant_vendors.models import TenantVendorLink
from app.modules.tenants.models import Tenant
from app.modules.users.models import Permission, Role, RolePermission, User, UserRole, UserToken
from app.modules.vendors.models import (
    FactSource,
    Vendor,
    VendorAlias,
    VendorCertification,
    VendorDomain,
    VendorEquipment,
    VendorIndustry,
    VendorLocation,
    VendorProject,
    VendorService,
    VendorSource,
)

__all__ = [
    "ApiKey",
    "ApprovalHistory",
    "AuditLog",
    "Base",
    "CertificationType",
    "EquipmentCategory",
    "FactSource",
    "ImportRow",
    "Industry",
    "Job",
    "KnowledgeProposal",
    "Permission",
    "ProposalItem",
    "RefreshToken",
    "Role",
    "RolePermission",
    "ServiceCategory",
    "Tenant",
    "TenantVendorLink",
    "Upload",
    "User",
    "UserRole",
    "UserToken",
    "Vendor",
    "VendorAlias",
    "VendorCertification",
    "VendorDomain",
    "VendorEquipment",
    "VendorIndustry",
    "VendorLocation",
    "VendorProject",
    "VendorService",
    "VendorSource",
]
