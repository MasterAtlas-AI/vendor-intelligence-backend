"""An organization's own project history (SOW 3, 5, 6): the equipment lines of its executed projects (from MELs),
procurement records, lessons learned and operating feedback. Everything here is tenant-private (FORCE'd RLS):
it links to the shared knowledge base (vendors, taxonomy) but is never visible to another organization.

Attribute definitions (capacity, pressure...) are shared defaults (tenant_id NULL) plus an organization's own.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base
from app.db.mixins import Entity, TenantScoped

# How far a record can be trusted (SOW 19). Shown to users on every record.
VERIFICATION = (
    "verified",  # Verified SPE data: checked by an authorized user
    "historical",  # Historical project data: from an executed project's MEL or records
    "vendor_provided",  # Vendor-provided data (datasheets, quotations)
    "public",  # Public-domain data
    "ai_extracted",  # AI- or rule-extracted, pending verification
    "user_entered",  # User-entered, pending verification
    "archived",  # Archived / superseded
)
ORIGINS = ("mel", "po", "datasheet", "tbe", "vendor_document", "manual", "extraction")
# inspection: inspection and QA/QC records (SOW 3.15); project_update: project-management notes (SOW 10.2)
NOTE_KINDS = (
    "lesson_learned",
    "operating_feedback",
    "maintenance",
    "spares",
    "performance",
    "inspection",
    "project_update",
    "other",
)
# Where the equipment line stands (SOW 14.8 project management: equipment status)
EQUIPMENT_STATUSES = (
    "planned",
    "ordered",
    "delivered",
    "installed",
    "in_service",
    "standby",
    "decommissioned",
)
# raised / approved: a requisition's stages before an order exists (SOW 3.6, 3.7)
PROCUREMENT_STATUSES = ("raised", "approved", "ordered", "manufacturing", "shipped", "delivered", "cancelled")
# pr: purchase requisition, mr: material requisition (SOW 3.6, 3.7); po; quotation (with its TBE result)
RECORD_TYPES = ("pr", "mr", "po", "quotation")
TECHNICAL_STATUSES = ("accepted", "conditional", "rejected")  # technical bid evaluation result
MEL_ROW_STATUSES = ("pending", "approved", "rejected")
ATTRIBUTE_TYPES = ("number", "text")
DEPARTMENTS = (
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
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ProjectEquipment(Entity, TenantScoped, Base):
    """One equipment line of an organization's project (a MEL row): what was supplied, by whom, to what rating."""

    __tablename__ = "project_equipment"
    __table_args__ = (
        CheckConstraint(_in("verification", VERIFICATION), name="verification"),
        CheckConstraint(_in("origin", ORIGINS), name="origin"),
        CheckConstraint(f"status IS NULL OR {_in('status', EQUIPMENT_STATUSES)}", name="status"),
        Index("ix_project_equipment_tenant_project", "tenant_id", "project_id"),
        Index("ix_project_equipment_tenant_category", "tenant_id", "category_id"),
        Index("ix_project_equipment_tenant_vendor", "tenant_id", "vendor_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant_projects.id", ondelete="CASCADE"))
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("equipment_categories.id", ondelete="SET NULL")
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    vendor_name: Mapped[str] = mapped_column(String(300), default="")  # as written in the source
    manufacturer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    manufacturer_name: Mapped[str] = mapped_column(String(300), default="")
    tag: Mapped[str] = mapped_column(String(120), default="")
    name: Mapped[str] = mapped_column(String(300))  # equipment description
    service: Mapped[str] = mapped_column(String(300), default="")  # duty / process fluid
    model: Mapped[str] = mapped_column(String(200), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # Attribute key -> number (canonical unit of its definition) or text. See EquipmentAttribute.
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    standards: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list)
    specs_text: Mapped[str] = mapped_column(Text, default="")  # anything else from the source, verbatim
    department: Mapped[str | None] = mapped_column(String(30))
    status: Mapped[str | None] = mapped_column(String(20))  # EQUIPMENT_STATUSES; unknown for history
    verification: Mapped[str] = mapped_column(String(20), default="historical")
    origin: Mapped[str] = mapped_column(String(20), default="manual")
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("uploads.id", ondelete="SET NULL"))
    source_ref: Mapped[str] = mapped_column(String(200), default="")  # "MEL rev C, sheet 1, row 42"
    notes: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)


class ProcurementRecord(Entity, TenantScoped, Base):
    """A purchase order and its delivery (SOW 14 planning/procurement): lead times, on-time delivery, rating."""

    __tablename__ = "procurement_records"
    __table_args__ = (
        CheckConstraint(_in("status", PROCUREMENT_STATUSES), name="status"),
        CheckConstraint("performance_rating IS NULL OR performance_rating BETWEEN 1 AND 5", name="rating"),
        CheckConstraint(_in("record_type", RECORD_TYPES), name="record_type"),
        CheckConstraint(
            f"technical_status IS NULL OR {_in('technical_status', TECHNICAL_STATUSES)}",
            name="technical_status",
        ),
        Index("ix_procurement_records_tenant_vendor", "tenant_id", "vendor_id"),
        Index("ix_procurement_records_tenant_project", "tenant_id", "project_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant_projects.id", ondelete="CASCADE"))
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_equipment.id", ondelete="SET NULL")
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    vendor_name: Mapped[str] = mapped_column(String(300), default="")
    record_type: Mapped[str] = mapped_column(String(12), default="po")  # RECORD_TYPES
    technical_status: Mapped[str | None] = mapped_column(
        String(12)
    )  # TBE result: accepted | conditional | rejected
    po_number: Mapped[str] = mapped_column(String(80), default="")
    po_date: Mapped[date | None] = mapped_column(Date)
    promised_date: Mapped[date | None] = mapped_column(Date)
    delivered_date: Mapped[date | None] = mapped_column(Date)
    lead_time_weeks: Mapped[int | None] = mapped_column(SmallInteger)
    manufacturing_weeks: Mapped[int | None] = mapped_column(SmallInteger)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))  # commercial: needs commercial:read
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    status: Mapped[str] = mapped_column(String(15), default="ordered")
    on_time: Mapped[bool | None] = mapped_column(Boolean)
    performance_rating: Mapped[int | None] = mapped_column(SmallInteger)  # 1-5
    notes: Mapped[str] = mapped_column(Text, default="")
    department: Mapped[str | None] = mapped_column(String(30))
    verification: Mapped[str] = mapped_column(String(20), default="historical")


class KnowledgeNote(Entity, TenantScoped, Base):
    """Lessons learned, operating feedback, maintenance and spares notes, tied to a project, vendor or equipment."""

    __tablename__ = "knowledge_notes"
    __table_args__ = (
        CheckConstraint(_in("kind", NOTE_KINDS), name="kind"),
        Index("ix_knowledge_notes_tenant_vendor", "tenant_id", "vendor_id"),
    )

    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    department: Mapped[str | None] = mapped_column(String(30))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenant_projects.id", ondelete="CASCADE"))
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_equipment.id", ondelete="CASCADE")
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("equipment_categories.id", ondelete="SET NULL")
    )
    verification: Mapped[str] = mapped_column(String(20), default="user_entered")


class EquipmentAttribute(Entity, Base):
    """A structured equipment attribute (SOW 5.2, 24): shared defaults (tenant_id NULL) plus the organization's own.
    `category_id` NULL applies to every category; otherwise to that category and everything under it."""

    __tablename__ = "equipment_attributes"
    __table_args__ = (
        CheckConstraint(_in("data_type", ATTRIBUTE_TYPES), name="data_type"),
        Index("ix_equipment_attributes_tenant_key", "tenant_id", "key"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("equipment_categories.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String(60))
    label: Mapped[str] = mapped_column(String(120))
    data_type: Mapped[str] = mapped_column(String(10), default="number")
    unit: Mapped[str] = mapped_column(String(20), default="")  # canonical unit values are stored in
    quantity: Mapped[str] = mapped_column(
        String(20), default=""
    )  # what units parse into it: flow, pressure...
    synonyms: Mapped[list[str]] = mapped_column(
        ARRAY(String(80)), default=list
    )  # column headers / spec labels
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)


class MelRow(Entity, TenantScoped, Base):
    """One row of an uploaded MEL, parsed and matched, waiting for review before it becomes ProjectEquipment."""

    __tablename__ = "mel_rows"
    __table_args__ = (
        CheckConstraint(_in("status", MEL_ROW_STATUSES), name="status"),
        Index("ix_mel_rows_upload_status", "upload_id", "status"),
    )

    upload_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant_projects.id", ondelete="CASCADE"))
    row_number: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict)  # header -> cell, as in the file
    parsed: Mapped[dict[str, Any]] = mapped_column(
        default=dict
    )  # the ProjectEquipment fields it would create
    issues: Mapped[list[str]] = mapped_column(ARRAY(String(200)), default=list)
    status: Mapped[str] = mapped_column(String(10), default="pending")
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_equipment.id", ondelete="SET NULL")
    )


class SavedSearch(Entity, Base):
    """A named search a user keeps (SOW 15): vendor or equipment search with its filters."""

    __tablename__ = "saved_searches"
    __table_args__ = (
        CheckConstraint("kind IN ('vendors', 'equipment')", name="kind"),
        Index("ix_saved_searches_tenant_user", "tenant_id", "user_id"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(120))
    query: Mapped[str] = mapped_column(String(500), default="")
    filters: Mapped[dict[str, Any]] = mapped_column(default=dict)


# SOW 3.13 / 3.14 / 3.17 / 14.6: a project's documents beside its equipment history.
DOCUMENT_TYPES = (
    "drawing",
    "datasheet",
    "specification",
    "correspondence",
    "schedule",
    "report",
    "certificate",
    "other",
)


class ProjectDocument(Entity, TenantScoped, Base):
    """A project document (vendor drawing, letter, schedule, datasheet...): numbered, revised, optionally tied to an
    equipment line and a vendor, with the file itself. Tenant-private like the rest of project history."""

    __tablename__ = "project_documents"
    __table_args__ = (
        CheckConstraint(_in("doc_type", DOCUMENT_TYPES), name="doc_type"),
        Index("ix_project_documents_tenant_project", "tenant_id", "project_id"),
        Index("ix_project_documents_equipment", "equipment_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant_projects.id", ondelete="CASCADE"))
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_equipment.id", ondelete="SET NULL")
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    doc_type: Mapped[str] = mapped_column(String(20))
    doc_number: Mapped[str] = mapped_column(String(120), default="")  # e.g. 20171-SPOG-62400-MA-DW-0002
    title: Mapped[str] = mapped_column(String(300))
    revision: Mapped[str] = mapped_column(String(20), default="")
    doc_date: Mapped[date | None] = mapped_column(Date)
    department: Mapped[str | None] = mapped_column(String(30))
    notes: Mapped[str] = mapped_column(Text, default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))
    # Words read from the file on the server (SOW H.4 / 25.4); None until read, '' when the file has none.
    text_content: Mapped[str | None] = deferred(mapped_column(Text))
    text_chars: Mapped[int] = mapped_column(Integer, default=-1, server_default="-1")  # -1: not read yet
