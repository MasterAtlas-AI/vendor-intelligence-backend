import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Verification = Literal[
    "verified", "historical", "vendor_provided", "public", "ai_extracted", "user_entered", "archived"
]
Origin = Literal["mel", "po", "datasheet", "tbe", "vendor_document", "manual", "extraction"]
NoteKind = Literal[
    "lesson_learned",
    "operating_feedback",
    "maintenance",
    "spares",
    "performance",
    "inspection",
    "project_update",
    "other",
]
EquipmentStatus = Literal[
    "planned", "ordered", "delivered", "installed", "in_service", "standby", "decommissioned"
]
ProcurementStatus = Literal["ordered", "manufacturing", "shipped", "delivered", "cancelled"]
RecordType = Literal["po", "quotation"]
TechnicalStatus = Literal["accepted", "conditional", "rejected"]
Department = Literal[
    "engineering",
    "procurement",
    "planning",
    "project_management",
    "operations",
    "construction",
    "commissioning",
    "qa_qc",
    "hse",
    "supply_chain",
    "management",
    "business_development",
    "marketing",
    "document_control",
]
AttrValue = float | list[float] | str  # a number, a [min, max] range, or text


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------- equipment lines


class EquipmentWrite(_In):
    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=300)
    category_code: str | None = Field(default=None, max_length=160)
    vendor_id: uuid.UUID | None = None
    vendor_name: str = Field(default="", max_length=300)
    manufacturer_id: uuid.UUID | None = None
    manufacturer_name: str = Field(default="", max_length=300)
    tag: str = Field(default="", max_length=120)
    service: str = Field(default="", max_length=300)
    model: str = Field(default="", max_length=200)
    quantity: int = Field(default=1, ge=1, le=100_000)
    attributes: dict[str, AttrValue] = Field(default_factory=dict, max_length=60)
    standards: list[str] = Field(default_factory=list, max_length=20)
    specs_text: str = Field(default="", max_length=20_000)
    department: Department | None = None
    status: EquipmentStatus | None = None
    verification: Verification = "user_entered"
    origin: Origin = "manual"
    source_ref: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=5000)


class EquipmentUpdate(_In):
    version: int
    name: str | None = Field(default=None, min_length=1, max_length=300)
    category_code: str | None = Field(default=None, max_length=160)
    vendor_id: uuid.UUID | None = None
    vendor_name: str | None = Field(default=None, max_length=300)
    manufacturer_id: uuid.UUID | None = None
    manufacturer_name: str | None = Field(default=None, max_length=300)
    tag: str | None = Field(default=None, max_length=120)
    service: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=200)
    quantity: int | None = Field(default=None, ge=1, le=100_000)
    attributes: dict[str, AttrValue] | None = Field(default=None, max_length=60)
    standards: list[str] | None = Field(default=None, max_length=20)
    specs_text: str | None = Field(default=None, max_length=20_000)
    department: Department | None = None
    status: EquipmentStatus | None = None
    verification: Verification | None = None
    notes: str | None = Field(default=None, max_length=5000)


class ProjectRef(BaseModel):
    id: uuid.UUID
    name: str
    code: str | None
    client: str | None
    facility_type: str | None
    country: str | None
    year: int | None
    location: str | None
    kind: str


class ProjectEquipmentRead(BaseModel):
    id: uuid.UUID
    project: ProjectRef
    category_code: str | None
    category_name: str | None
    vendor_id: uuid.UUID | None
    vendor_name: str  # the linked vendor's name, else the name as written in the source
    manufacturer_id: uuid.UUID | None
    manufacturer_name: str
    tag: str
    name: str
    service: str
    model: str
    quantity: int
    attributes: dict[str, Any]
    standards: list[str]
    specs_text: str
    department: str | None
    status: EquipmentStatus | None = None
    verification: Verification
    origin: Origin
    source_ref: str
    has_source_file: bool = False  # the original upload can be downloaded (GET /equipment/{id}/source)
    notes: str
    version: int
    created_at: datetime


class EquipmentSearch(_In):
    q: str = Field(default="", max_length=200)  # tag, description, service, model, vendor
    project_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    category_code: str | None = None  # this category and everything under it
    vendor_id: uuid.UUID | None = None  # supplied by, or made by (OEM), this vendor
    service: str = Field(default="", max_length=120)  # duty / fluid ("seawater"), within the category
    statuses: list[EquipmentStatus] = Field(default_factory=list, max_length=7)
    countries: list[str] = Field(default_factory=list, max_length=30)  # project country
    facility_types: list[str] = Field(default_factory=list, max_length=20)
    location: Literal["offshore", "onshore"] | None = None
    year_from: int | None = Field(default=None, ge=1900, le=2100)
    year_to: int | None = Field(default=None, ge=1900, le=2100)
    standard: str | None = Field(default=None, max_length=80)
    material: str | None = Field(default=None, max_length=80)
    verification: list[Verification] = Field(default_factory=list)
    department: Department | None = None
    # attribute key -> [min, max] (either may be null): numeric filters in the attribute's unit
    ranges: dict[str, list[float | None]] = Field(default_factory=dict, max_length=10)
    include_archived: bool = False
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class EquipmentPage(BaseModel):
    items: list[ProjectEquipmentRead]
    total: int


# ---------------------------------------------------------------- procurement and notes


class ProcurementWrite(_In):
    project_id: uuid.UUID
    equipment_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    vendor_name: str = Field(default="", max_length=300)
    record_type: RecordType = "po"
    technical_status: TechnicalStatus | None = None  # technical bid evaluation result
    po_number: str = Field(default="", max_length=80)
    po_date: date | None = None
    promised_date: date | None = None
    delivered_date: date | None = None
    lead_time_weeks: int | None = Field(default=None, ge=0, le=520)
    manufacturing_weeks: int | None = Field(default=None, ge=0, le=520)
    amount: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    status: ProcurementStatus = "ordered"
    performance_rating: int | None = Field(default=None, ge=1, le=5)
    notes: str = Field(default="", max_length=5000)
    department: Department | None = None
    verification: Verification = "user_entered"


class ProcurementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    project_name: str = ""
    equipment_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    vendor_name: str
    record_type: RecordType = "po"
    technical_status: TechnicalStatus | None = None
    po_number: str
    po_date: date | None
    promised_date: date | None
    delivered_date: date | None
    lead_time_weeks: int | None
    manufacturing_weeks: int | None
    amount: Decimal | None  # null unless the caller has commercial:read
    currency: str | None
    status: ProcurementStatus
    on_time: bool | None
    performance_rating: int | None
    notes: str
    department: str | None
    verification: Verification
    commercial_hidden: bool = False


class NoteWrite(_In):
    kind: NoteKind
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=20_000)
    department: Department | None = None
    project_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    equipment_id: uuid.UUID | None = None
    category_code: str | None = None
    verification: Verification = "user_entered"


class NoteRead(BaseModel):
    id: uuid.UUID
    kind: NoteKind
    title: str
    body: str
    department: str | None
    project_id: uuid.UUID | None
    project_name: str | None
    vendor_id: uuid.UUID | None
    equipment_id: uuid.UUID | None
    category_code: str | None
    verification: Verification
    created_at: datetime


# ---------------------------------------------------------------- vendor history (SPE experience)


class VendorHistory(BaseModel):
    """What the organization's own records say about a vendor (SOW 5.1 "previous SPE experience")."""

    vendor_id: uuid.UUID
    projects: list[ProjectRef]
    equipment_count: int
    categories: list[str]
    purchase_orders: int
    quotations: int = 0
    technically_accepted: int = 0  # technical bid evaluations it passed (accepted or conditional)
    technically_rejected: int = 0
    avg_lead_time_weeks: float | None
    on_time_rate: float | None  # 0-1, delivered orders with a promised date
    avg_rating: float | None  # 1-5
    notes: list[NoteRead]


# ---------------------------------------------------------------- attributes


class AttributeRead(BaseModel):
    id: uuid.UUID
    key: str
    label: str
    data_type: Literal["number", "text"]
    unit: str
    quantity: str
    synonyms: list[str]
    category_code: str | None
    shared: bool  # a platform default (read-only for organizations)
    sort_order: int


class AttributeWrite(_In):
    key: str = Field(min_length=2, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=120)
    quantity: Literal[
        "", "flow", "head", "pressure", "temperature", "power", "mass", "area", "voltage", "volume"
    ] = ""
    synonyms: list[str] = Field(default_factory=list, max_length=20)
    category_code: str | None = None
    sort_order: int = Field(default=100, ge=0, le=1000)


# ---------------------------------------------------------------- MEL import


class MelSheet(BaseModel):
    name: str
    header_row: int
    headers: list[str]
    data_rows: int
    sample: list[list[str]]
    suggested_mapping: dict[str, int]  # field -> column index (fields: MEL_FIELDS and attribute keys)


class MelUploadRead(BaseModel):
    upload_id: uuid.UUID
    project_id: uuid.UUID
    filename: str
    sheets: list[MelSheet]
    fields: dict[str, str]  # every mappable field -> its label


class MelUploadSummary(BaseModel):
    """A MEL uploaded to a project, with where its review stands."""

    upload_id: uuid.UUID
    filename: str
    uploaded_at: datetime
    job_id: uuid.UUID | None  # the latest parse
    job_status: str | None
    counts: dict[str, int]  # rows by status: pending / approved / rejected


class MelParseRequest(_In):
    project_id: uuid.UUID
    sheet: str | None = None
    header_row: int | None = Field(default=None, ge=1)
    mapping: dict[str, int] | None = None  # default: the suggested mapping


class MelRowRead(BaseModel):
    id: uuid.UUID
    row_number: int
    raw: dict[str, Any]
    parsed: dict[str, Any]
    issues: list[str]
    status: Literal["pending", "approved", "rejected"]
    equipment_id: uuid.UUID | None


class MelRows(BaseModel):
    upload_id: uuid.UUID
    project_id: uuid.UUID
    filename: str
    counts: dict[str, int]
    rows: list[MelRowRead]


class MelRowUpdate(_In):
    name: str | None = Field(default=None, max_length=300)
    tag: str | None = Field(default=None, max_length=120)
    category_code: str | None = None
    vendor_id: uuid.UUID | None = None
    vendor_name: str | None = Field(default=None, max_length=300)
    manufacturer_id: uuid.UUID | None = None
    manufacturer_name: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=200)
    attributes: dict[str, AttrValue] | None = None


class MelDecision(_In):
    row_ids: list[uuid.UUID] = Field(min_length=1, max_length=2000)
    decision: Literal["approve", "reject"]
    verification: Literal["historical", "verified"] = "historical"


class MelDecisionResult(BaseModel):
    approved: int
    rejected: int
    skipped: int  # already decided, or not found
    vendors_linked: int  # matched vendors added to the organization's vendor list


# ---------------------------------------------------------------- similarity / near-fit


class SimilarRequest(_In):
    category_code: str | None = None
    service: str = Field(default="", max_length=120)  # duty / fluid: lines in the same service score higher
    attributes: dict[str, float] = Field(
        default_factory=dict, max_length=10
    )  # target values, canonical units
    standards: list[str] = Field(default_factory=list, max_length=10)
    estimate: list[str] = Field(default_factory=list, max_length=5)  # attribute keys to interpolate
    include_catalogue: bool = True  # also compare vendors' catalogue products (public data)
    limit: int = Field(default=20, ge=1, le=100)


class AttributeDiff(BaseModel):
    key: str
    requested: float
    found: float | list[float] | None
    difference_pct: float | None  # signed, relative to the request; 0 when within a catalogue range
    note: str  # "within range", "+12% above", "not on record"


class SimilarMatch(BaseModel):
    source: Literal["spe_history", "catalogue"]
    # Exact historical data / direct vendor data / comparable reference (SOW 12): never an approved decision.
    match: Literal["exact", "comparable"]
    score: float  # 0-1
    equipment_id: uuid.UUID | None  # spe_history
    product_id: uuid.UUID | None  # catalogue
    vendor_id: uuid.UUID | None
    vendor_name: str
    name: str
    model: str
    category_code: str | None
    category_name: str | None
    project: ProjectRef | None
    standards: list[str]
    diffs: list[AttributeDiff]
    verification: str  # the record's verification status ("public" for catalogue data)


class Estimate(BaseModel):
    """An interpolated or extrapolated value: an idea to check, never verified vendor data (SOW 13)."""

    key: str
    label: str
    value: float
    unit: str
    method: Literal["interpolated", "extrapolated", "nearest"]
    driver: str  # the attribute it was estimated from
    basis: list[str]  # the reference records used
    label_note: str = "Estimate from historical records: not verified vendor data"


class SimilarResult(BaseModel):
    matches: list[SimilarMatch]
    estimates: list[Estimate]
    notice: str = (
        "Suggestions from historical and catalogue data. They are not approved engineering decisions; "
        "final technical and commercial decisions remain with the responsible engineer."
    )


# ---------------------------------------------------------------- saved searches and the category browser


class SavedSearchWrite(_In):
    kind: Literal["vendors", "equipment"]
    name: str = Field(min_length=1, max_length=120)
    query: str = Field(default="", max_length=500)
    filters: dict[str, Any] = Field(default_factory=dict)


class SavedSearchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: Literal["vendors", "equipment"]
    name: str
    query: str
    filters: dict[str, Any]
    created_at: datetime


class CategoryNode(BaseModel):
    code: str
    name: str
    level: int
    standards: list[str]
    vendor_count: int  # vendors in the shared knowledge base with a product in this category (or under it)
    equipment_count: int  # the organization's own equipment lines in this category (or under it)
    children: list["CategoryNode"] = Field(default_factory=list)
