"""Imports every model so `Base.metadata` is complete (Alembic, relationship resolution)."""

from app.ai.models import AiUsageEvent
from app.crawler.models import WebPage
from app.db.base import Base
from app.modules.api_keys.models import ApiKey
from app.modules.assistant.models import AssistantConversation, AssistantMessage
from app.modules.audit.models import AuditLog
from app.modules.auth.models import RefreshToken
from app.modules.equipment.models import (
    EquipmentAttribute,
    KnowledgeNote,
    MelRow,
    ProcurementRecord,
    ProjectEquipment,
    SavedSearch,
)
from app.modules.imports.models import ImportRow, Upload
from app.modules.jobs.models import Job
from app.modules.projects.models import Project, ProjectMember, ProjectVendor, Scorecard, ScorecardTemplate
from app.modules.proposals.models import ApprovalHistory, KnowledgeProposal, ProposalItem
from app.modules.reports.models import Report
from app.modules.rfqs.models import Rfq, RfqVendor
from app.modules.search.models import SearchHistory, VendorEmbedding
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
from app.modules.webhooks.models import RateCounter, WebhookDelivery, WebhookEndpoint

__all__ = [
    "AiUsageEvent",
    "ApiKey",
    "ApprovalHistory",
    "AssistantConversation",
    "AssistantMessage",
    "AuditLog",
    "Base",
    "CertificationType",
    "EquipmentAttribute",
    "EquipmentCategory",
    "FactSource",
    "ImportRow",
    "Industry",
    "Job",
    "KnowledgeNote",
    "KnowledgeProposal",
    "MelRow",
    "Permission",
    "ProcurementRecord",
    "Project",
    "ProjectEquipment",
    "ProjectMember",
    "ProjectVendor",
    "ProposalItem",
    "RateCounter",
    "RefreshToken",
    "Report",
    "Rfq",
    "RfqVendor",
    "Role",
    "RolePermission",
    "SavedSearch",
    "Scorecard",
    "ScorecardTemplate",
    "SearchHistory",
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
    "VendorEmbedding",
    "VendorEquipment",
    "VendorIndustry",
    "VendorLocation",
    "VendorProject",
    "VendorService",
    "VendorSource",
    "WebPage",
    "WebhookDelivery",
    "WebhookEndpoint",
]
