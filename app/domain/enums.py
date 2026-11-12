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
    WEBHOOK_DELIVERY = "webhook_delivery"
    WEBSITE_LOOKUP = "website_lookup"  # earlier name of VENDOR_LOOKUP, kept for existing job rows
    VENDOR_LOOKUP = "vendor_lookup"
    PRODUCT_CATALOG = "product_catalog"
    MEL_IMPORT = "mel_import"


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
    MERGE_VENDORS = "merge_vendors"  # two records are the same company: fold one into the other


class ProposalOrigin(StrEnum):
    MANUAL = "manual"  # entered by a curator
    EXCEL = "excel"
    PDF = "pdf"
    AI_VENDOR_DISCOVERY = "ai_vendor_discovery"
    AI_EQUIPMENT_DISCOVERY = "ai_equipment_discovery"
    SUGGESTION = "suggestion"  # a tenant user's correction
    DEDUP_SCAN = "dedup_scan"  # the duplicate scan job


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
    ADD_CONTACT = "add_contact"  # a company contact point (role mailbox or switchboard), never a named person
    MERGE_VENDOR = "merge_vendor"  # payload: duplicate_vendor_id -> folded into the proposal's subject vendor


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


# ---------------------------------------------------------------- imports (Phase 2)


class UploadKind(StrEnum):
    SPREADSHEET = "spreadsheet"  # .xlsx / .xlsm / .csv
    PDF = "pdf"


class ImportRowOutcome(StrEnum):
    """What an import did with one row of the file (shown in the import report)."""

    MATCHED = "matched"  # an existing vendor: added to the tenant's list (new facts proposed separately)
    CREATED = "created"  # a new vendor, auto-approved (trusted tenant) and added to the tenant's list
    PROPOSED = "proposed"  # a new vendor proposal awaits curator review; linked to the tenant when approved
    ALREADY_PROPOSED = (
        "already_proposed"  # an open proposal for this vendor exists; linked when it's approved
    )
    DUPLICATE_IN_FILE = "duplicate_in_file"  # same vendor as an earlier row
    AMBIGUOUS = "ambiguous"  # the name matches several vendors; add a website to disambiguate
    INVALID = "invalid"  # the row can't be used (missing name, bad website/country, ...)
    DISCARDED = "discarded"  # AI discovery: looked at, not proposed (not relevant, no evidence, unreachable)
