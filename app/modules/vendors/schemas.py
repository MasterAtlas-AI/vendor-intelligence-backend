import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import AvlStatus, LocationType, SourceKind, VendorStatus


class TenantOverlayRead(BaseModel):
    """The caller's tenant-private view of the vendor (absent when the tenant has no record yet)."""

    model_config = ConfigDict(from_attributes=True)
    avl_status: AvlStatus
    internal_vendor_code: str | None
    tags: list[str]
    notes: str
    updated_at: datetime


class VendorSummary(BaseModel):
    id: uuid.UUID
    display_name: str
    legal_name: str
    website: str | None
    hq_country: str | None
    hq_city: str | None
    status: VendorStatus
    equipment_count: int
    service_count: int
    certification_count: int
    tenant: TenantOverlayRead | None = None


class CategoryRef(BaseModel):
    id: uuid.UUID
    code: str
    name: str


class EquipmentRead(BaseModel):
    id: uuid.UUID
    category: CategoryRef
    name: str
    model_series: str | None
    standards: list[str]
    specs: dict[str, Any]
    applications: list[str]
    # Read from `specs` by rules (attribute key -> [min, max]); shown as extracted, pending verification.
    attributes: dict[str, Any] = {}


class ServiceRead(BaseModel):
    id: uuid.UUID
    category: CategoryRef
    equipment_category: CategoryRef | None
    name: str
    regions: list[str]


class CertificationRead(BaseModel):
    id: uuid.UUID
    certification: CategoryRef
    certificate_number: str | None
    scope: str
    valid_from: date | None
    valid_to: date | None
    verified: bool


class LocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    location_type: LocationType
    address: str
    city: str | None
    country: str | None


class ContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    contact_type: str
    label: str
    email: str | None
    phone: str | None
    country: str | None


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_name: str
    client: str | None
    operator: str | None
    facility_type: str | None
    country: str | None
    year: int | None
    scope: str


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: SourceKind
    url: str | None
    title: str
    retrieved_at: datetime | None


class VendorDetail(VendorSummary):
    description: str
    parent_company: str | None = None
    domains: list[str]
    aliases: list[str]
    industries: list[CategoryRef]
    data_quality_score: Decimal | None
    last_verified_at: datetime | None
    knowledge_version: int
    locations: list[LocationRead]
    contacts: list[ContactRead] = []
    equipment: list[EquipmentRead]
    services: list[ServiceRead]
    certifications: list[CertificationRead]
    projects: list[ProjectRead]
    sources: list[SourceRead]


class VendorPage(BaseModel):
    items: list[VendorSummary]
    next_cursor: str | None
    has_more: bool


class TenantOverlayUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    avl_status: AvlStatus | None = None
    internal_vendor_code: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = Field(default=None, max_length=30)
    notes: str | None = Field(default=None, max_length=5000)
