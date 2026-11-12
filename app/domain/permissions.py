"""Permission catalogue and role bundles.

Tenant roles are stored in the database (seeded from TENANT_ROLES by `app.cli bootstrap`) so tenants can
later define custom roles. Platform roles are code-defined and assigned through `users.platform_roles`.
"""

from enum import StrEnum


class P(StrEnum):
    TENANT_MANAGE = "tenant:manage"
    TENANT_SETTINGS_MANAGE = "tenant_settings:manage"
    KNOWLEDGE_APPROVE = "knowledge:approve"
    TAXONOMY_MANAGE = "taxonomy:manage"
    USER_READ = "user:read"
    USER_MANAGE = "user:manage"
    ROLE_MANAGE = "role:manage"
    APIKEY_MANAGE = "apikey:manage"
    AUDIT_READ = "audit:read"
    UPLOAD_CREATE = "upload:create"
    DISCOVERY_RUN = "discovery:run"
    PROJECT_MANAGE = "project:manage"
    RFQ_MANAGE = "rfq:manage"
    SCORECARD_MANAGE = "scorecard:manage"
    VENDOR_READ = "vendor:read"
    SEARCH_RUN = "search:run"
    REPORT_READ = "report:read"
    TENANT_VENDOR_APPROVE = "tenant_vendor:approve"
    EQUIPMENT_READ = "equipment:read"
    EQUIPMENT_MANAGE = "equipment:manage"
    COMMERCIAL_READ = "commercial:read"
    DATA_EXPORT = "data:export"


PERMISSION_DESCRIPTIONS: dict[P, str] = {
    P.TENANT_MANAGE: "Manage all tenants on the platform",
    P.TENANT_SETTINGS_MANAGE: "Manage own tenant profile and settings",
    P.KNOWLEDGE_APPROVE: "Approve or reject global knowledge proposals",
    P.TAXONOMY_MANAGE: "Manage equipment/service taxonomy",
    P.USER_READ: "View users and roles in the tenant",
    P.USER_MANAGE: "Invite, edit, disable users and assign roles",
    P.ROLE_MANAGE: "Create and edit custom roles",
    P.APIKEY_MANAGE: "Create and revoke API keys",
    P.AUDIT_READ: "Read the tenant audit log and all tenant jobs",
    P.UPLOAD_CREATE: "Upload Excel/PDF files and run imports",
    P.DISCOVERY_RUN: "Run AI discovery jobs",
    P.PROJECT_MANAGE: "Create and manage projects",
    P.RFQ_MANAGE: "Create and manage RFQs",
    P.SCORECARD_MANAGE: "Create and manage vendor scorecards",
    P.VENDOR_READ: "Read the vendor knowledge base",
    P.SEARCH_RUN: "Run vendor searches",
    P.REPORT_READ: "View and download reports",
    P.TENANT_VENDOR_APPROVE: "Set the tenant's own vendor approval (AVL) status",
    P.EQUIPMENT_READ: "Read the organization's own project history: equipment lines, procurement records, notes",
    P.EQUIPMENT_MANAGE: "Import MELs, approve and edit the organization's project history and attribute definitions",
    P.COMMERCIAL_READ: "See commercial values: PO amounts, quotations and prices",
    P.DATA_EXPORT: "Export data (CSV and Excel downloads)",
}

_READ = {P.VENDOR_READ, P.SEARCH_RUN, P.REPORT_READ, P.EQUIPMENT_READ}
_WORK = {P.UPLOAD_CREATE, P.DISCOVERY_RUN, P.RFQ_MANAGE, P.SCORECARD_MANAGE, P.DATA_EXPORT}
_DATA_ADMIN = {P.EQUIPMENT_MANAGE, P.TENANT_VENDOR_APPROVE, P.UPLOAD_CREATE, P.DISCOVERY_RUN, P.DATA_EXPORT}

TENANT_ROLES: dict[str, tuple[str, frozenset[P]]] = {
    "tenant_admin": (
        "Tenant Admin",
        frozenset(
            _READ
            | _WORK
            | {
                P.TENANT_SETTINGS_MANAGE,
                P.USER_READ,
                P.USER_MANAGE,
                P.ROLE_MANAGE,
                P.APIKEY_MANAGE,
                P.AUDIT_READ,
                P.PROJECT_MANAGE,
                P.TENANT_VENDOR_APPROVE,
                P.EQUIPMENT_MANAGE,
                P.COMMERCIAL_READ,
            }
        ),
    ),
    "manager": (
        "Manager",
        frozenset(
            _READ
            | _WORK
            | {P.USER_READ, P.AUDIT_READ, P.PROJECT_MANAGE, P.TENANT_VENDOR_APPROVE, P.COMMERCIAL_READ}
        ),
    ),
    "engineer": ("Engineer", frozenset(_READ | _WORK)),
    "viewer": ("Viewer", frozenset(_READ)),
    # Department roles (SOW 20): same building blocks, different reach into commercial data and administration.
    "data_admin": ("Data Administrator", frozenset(_READ | _DATA_ADMIN | {P.USER_READ, P.AUDIT_READ})),
    "procurement": (
        "Procurement User",
        frozenset(_READ | _WORK | {P.COMMERCIAL_READ, P.TENANT_VENDOR_APPROVE}),
    ),
    "planning": ("Planning User", frozenset(_READ | {P.COMMERCIAL_READ, P.DATA_EXPORT})),
    "project": ("Project User", frozenset(_READ | _WORK | {P.PROJECT_MANAGE, P.COMMERCIAL_READ})),
    "operations": ("Operations User", frozenset(_READ)),
    "management": (
        "Management User",
        frozenset(_READ | {P.COMMERCIAL_READ, P.DATA_EXPORT, P.AUDIT_READ, P.PROJECT_MANAGE}),
    ),
}

# What an organization may put in a custom role: everything its system roles can hold, nothing platform-only.
TENANT_ASSIGNABLE: frozenset[P] = frozenset().union(*(perms for _, perms in TENANT_ROLES.values()))

PLATFORM_ROLES: dict[str, frozenset[P]] = {
    "super_admin": frozenset(P),
    "curator": frozenset(_READ | {P.KNOWLEDGE_APPROVE, P.TAXONOMY_MANAGE, P.UPLOAD_CREATE, P.DISCOVERY_RUN}),
}

# Scopes an API key may carry: integration-safe permissions only (no user/key administration).
API_KEY_ALLOWED_SCOPES: frozenset[P] = frozenset(
    _READ
    | {P.UPLOAD_CREATE, P.DISCOVERY_RUN, P.USER_READ, P.EQUIPMENT_MANAGE, P.COMMERCIAL_READ, P.DATA_EXPORT}
)


def permissions_for_platform_roles(roles: list[str] | tuple[str, ...]) -> frozenset[str]:
    out: set[str] = set()
    for role in roles:
        out |= PLATFORM_ROLES.get(role, frozenset())
    return frozenset(out)
