"""Global vendor knowledge base. One row per real company, shared by all tenants; no tenant_id.

Canonical tables hold approved knowledge only. New vendors and new facts arrive as proposals (slice 2) and are
written here when a curator approves them. Facts are retired, never hard-deleted, so history and sources survive.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    CHAR,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CITEXT, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, pg_enum
from app.db.mixins import Entity
from app.domain.enums import FactStatus, LocationType, SourceKind, VendorStatus

fact_status = pg_enum(FactStatus, "fact_status")


class Vendor(Entity, Base):
    __tablename__ = "vendors"
    __table_args__ = (
        Index(
            "ix_vendors_normalized_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
        Index("ix_vendors_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_vendors_hq_country", "hq_country"),
    )

    legal_name: Mapped[str] = mapped_column(String(300))
    display_name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(300), index=True)
    website: Mapped[str | None] = mapped_column(String(500))
    hq_country: Mapped[str | None] = mapped_column(CHAR(2))
    hq_city: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[VendorStatus] = mapped_column(
        pg_enum(VendorStatus, "vendor_status"), default=VendorStatus.ACTIVE
    )
    parent_vendor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    # Set when this record was a duplicate folded into another one (status becomes retired).
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    data_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    knowledge_version: Mapped[int] = mapped_column(Integer, default=1)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple', coalesce(display_name, '') || ' ' || coalesce(legal_name, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(description, '')), 'C')",
            persisted=True,
        ),
        deferred=True,
    )

    domains: Mapped[list["VendorDomain"]] = relationship(lazy="selectin", order_by="VendorDomain.domain")
    aliases: Mapped[list["VendorAlias"]] = relationship(lazy="selectin", order_by="VendorAlias.alias")


class VendorDomain(Entity, Base):
    """Registrable web domains of a vendor. Unique across the KB: the strongest duplicate signal."""

    __tablename__ = "vendor_domains"

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(CITEXT, unique=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)


class VendorAlias(Entity, Base):
    __tablename__ = "vendor_aliases"
    __table_args__ = (
        UniqueConstraint("vendor_id", "normalized_alias"),
        Index(
            "ix_vendor_aliases_normalized_alias_trgm",
            "normalized_alias",
            postgresql_using="gin",
            postgresql_ops={"normalized_alias": "gin_trgm_ops"},
        ),
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(300))
    normalized_alias: Mapped[str] = mapped_column(String(300))
    alias_type: Mapped[str] = mapped_column(
        String(20), default="trade"
    )  # trade | former | subsidiary | abbrev


class VendorLocation(Entity, Base):
    __tablename__ = "vendor_locations"

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    location_type: Mapped[LocationType] = mapped_column(pg_enum(LocationType, "location_type"))
    address: Mapped[str] = mapped_column(Text, default="")
    city: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(CHAR(2), index=True)
    status: Mapped[FactStatus] = mapped_column(fact_status, default=FactStatus.APPROVED)


class VendorContact(Entity, Base):
    """A company contact point the vendor publishes: a role mailbox (sales@, info@) and/or a phone line. By policy
    there are no named people here (personal data); the payload validator refuses personal-looking mailboxes."""

    __tablename__ = "vendor_contacts"

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    contact_type: Mapped[str] = mapped_column(
        String(20)
    )  # general, sales, service, support, procurement, other
    label: Mapped[str] = mapped_column(String(120), default="")  # e.g. "Singapore office", "Spare parts"
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(40))
    country: Mapped[str | None] = mapped_column(CHAR(2))
    status: Mapped[FactStatus] = mapped_column(fact_status, default=FactStatus.APPROVED)


class VendorIndustry(Base):
    __tablename__ = "vendor_industries"

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vendors.id", ondelete="CASCADE"), primary_key=True
    )
    industry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("industries.id", ondelete="RESTRICT"), primary_key=True, index=True
    )


class VendorEquipment(Entity, Base):
    __tablename__ = "vendor_equipment"
    __table_args__ = (Index("ix_vendor_equipment_category_vendor", "category_id", "vendor_id"),)

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("equipment_categories.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(300))
    model_series: Mapped[str | None] = mapped_column(String(200))
    standards: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list)
    specs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    applications: Mapped[list[str]] = mapped_column(ARRAY(String(120)), default=list)
    status: Mapped[FactStatus] = mapped_column(fact_status, default=FactStatus.APPROVED)


class VendorService(Entity, Base):
    __tablename__ = "vendor_services"
    __table_args__ = (Index("ix_vendor_services_category_vendor", "category_id", "vendor_id"),)

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("service_categories.id", ondelete="RESTRICT"))
    equipment_category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("equipment_categories.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(300))
    regions: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[FactStatus] = mapped_column(fact_status, default=FactStatus.APPROVED)


class VendorCertification(Entity, Base):
    __tablename__ = "vendor_certifications"
    __table_args__ = (Index("ix_vendor_certifications_type_vendor", "certification_type_id", "vendor_id"),)

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    certification_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("certification_types.id", ondelete="RESTRICT")
    )
    certificate_number: Mapped[str | None] = mapped_column(String(120))
    scope: Mapped[str] = mapped_column(Text, default="")
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[FactStatus] = mapped_column(fact_status, default=FactStatus.APPROVED)


class VendorProject(Entity, Base):
    __tablename__ = "vendor_projects"

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    project_name: Mapped[str] = mapped_column(String(300))
    client: Mapped[str | None] = mapped_column(String(200))
    operator: Mapped[str | None] = mapped_column(String(200))
    facility_type: Mapped[str | None] = mapped_column(String(60))  # FPSO, refinery, LNG, ...
    country: Mapped[str | None] = mapped_column(CHAR(2))
    year: Mapped[int | None] = mapped_column(SmallInteger)
    scope: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[FactStatus] = mapped_column(fact_status, default=FactStatus.APPROVED)


class VendorSource(Entity, Base):
    """Where knowledge about a vendor came from. Public sources only: which tenant uploaded a file is
    tenant-private and lives on the tenant overlay / proposal, never here."""

    __tablename__ = "vendor_sources"

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    kind: Mapped[SourceKind] = mapped_column(pg_enum(SourceKind, "source_kind"))
    url: Mapped[str | None] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(300), default="")
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reliability: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))


class FactSource(Entity, Base):
    """Evidence for one fact (equipment row, certification, ...): which source said it, and the snippet."""

    __tablename__ = "fact_sources"
    __table_args__ = (Index("ix_fact_sources_fact", "fact_table", "fact_id"),)

    fact_table: Mapped[str] = mapped_column(String(40))
    fact_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vendor_sources.id", ondelete="CASCADE"), index=True
    )
    snippet: Mapped[str] = mapped_column(Text, default="")
    page: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
