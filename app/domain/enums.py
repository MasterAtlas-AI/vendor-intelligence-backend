from enum import StrEnum


class TenantStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"

    @property
    def can_sign_in(self) -> bool:
        return self in {TenantStatus.TRIAL, TenantStatus.ACTIVE}


class UserStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    DISABLED = "disabled"


class UserTokenPurpose(StrEnum):
    INVITE = "invite"
    PASSWORD_RESET = "password_reset"  # noqa: S105 - enum label, not a secret
    EMAIL_VERIFY = "email_verify"


class ActorType(StrEnum):
    USER = "user"
    API_KEY = "api_key"
    SYSTEM = "system"
    AI = "ai"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.PARTIALLY_SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }


class JobType(StrEnum):
    DIAGNOSTICS = "diagnostics"
    EXCEL_IMPORT = "excel_import"
    PDF_IMPORT = "pdf_import"
    VENDOR_DISCOVERY = "vendor_discovery"
    EQUIPMENT_DISCOVERY = "equipment_discovery"
    SERVICE_DISCOVERY = "service_discovery"
    ENRICHMENT = "enrichment"
    DEDUP_SCAN = "dedup_scan"
    EMBEDDING = "embedding"
    REPORT = "report"


# ---------------------------------------------------------------- knowledge base (Phase 2)


class VendorStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"  # e.g. acquired, no longer trading under this name
    RETIRED = "retired"  # removed from the knowledge base (merged or rejected after the fact)


class FactStatus(StrEnum):
    """Canonical facts are always approved; `retired` keeps history instead of hard-deleting."""

    APPROVED = "approved"
    RETIRED = "retired"


class LocationType(StrEnum):
    HQ = "hq"
    FACTORY = "factory"
    SERVICE_CENTER = "service_center"
    SALES = "sales"
    RND = "rnd"
    OTHER = "other"


class SourceKind(StrEnum):
    WEB = "web"
    DOCUMENT = "document"
    EXCEL = "excel"
    MANUAL = "manual"
    AI = "ai"


class AvlStatus(StrEnum):
    """A tenant's own approval of a vendor (its Approved Vendor List), independent of the global KB."""

    APPROVED = "approved"
    CONDITIONAL = "conditional"
    NOT_APPROVED = "not_approved"
    BLACKLISTED = "blacklisted"
    UNASSESSED = "unassessed"


# ---------------------------------------------------------------- proposals & approvals (Phase 2)


class ProposalKind(StrEnum):
    NEW_VENDOR = "new_vendor"  # creates a vendor (first item is create_vendor)
    UPDATE_VENDOR = "update_vendor"  # adds or changes facts on an existing vendor


class ProposalOrigin(StrEnum):
    MANUAL = "manual"  # entered by a curator
    EXCEL = "excel"
    PDF = "pdf"
    AI_VENDOR_DISCOVERY = "ai_vendor_discovery"
    AI_EQUIPMENT_DISCOVERY = "ai_equipment_discovery"
    SUGGESTION = "suggestion"  # a tenant user's correction


class ProposalStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    NEEDS_INFO = "needs_info"
    APPROVED = "approved"
    PARTIALLY_APPROVED = "partially_approved"
    REJECTED = "rejected"
    AUTO_APPROVED = "auto_approved"

    @property
    def is_open(self) -> bool:
        return self in {ProposalStatus.PENDING_REVIEW, ProposalStatus.NEEDS_INFO}


class ProposalItemAction(StrEnum):
    CREATE_VENDOR = "create_vendor"
    UPDATE_VENDOR_FIELD = "update_vendor_field"
    ADD_DOMAIN = "add_domain"
    ADD_ALIAS = "add_alias"
    ADD_LOCATION = "add_location"
    ADD_INDUSTRY = "add_industry"
    ADD_EQUIPMENT = "add_equipment"
    ADD_SERVICE = "add_service"
    ADD_CERTIFICATION = "add_certification"
    ADD_PROJECT = "add_project"


class ProposalItemStatus(StrEnum):
    PENDING = "pending"
    REJECTED = "rejected"
    APPLIED = "applied"  # approved and written to the knowledge base


class ApprovalAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_INFO = "request_info"
    AUTO_APPROVE = "auto_approve"
    EDIT = "edit"
