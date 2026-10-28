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
