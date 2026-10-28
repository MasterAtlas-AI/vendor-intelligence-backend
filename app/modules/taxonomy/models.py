"""Controlled vocabularies shared by every tenant: equipment and service categories, certifications, industries.

Categories form trees. `code` is the dot-separated path ("pumps.centrifugal.api610.bb2"), so "all descendants of X"
is a prefix match on `code` without needing the ltree extension.
"""

import uuid

from sqlalchemy import ARRAY, ForeignKey, Index, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import Entity


class _CategoryColumns:
    code: Mapped[str] = mapped_column(String(160), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[int] = mapped_column(SmallInteger, default=0)
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)
    synonyms: Mapped[list[str]] = mapped_column(ARRAY(String(120)), default=list)


class EquipmentCategory(_CategoryColumns, Entity, Base):
    __tablename__ = "equipment_categories"
    __table_args__ = (
        Index("ix_equipment_categories_code_prefix", "code", postgresql_ops={"code": "text_pattern_ops"}),
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("equipment_categories.id", ondelete="RESTRICT"), index=True
    )
    standards: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list)


class ServiceCategory(_CategoryColumns, Entity, Base):
    __tablename__ = "service_categories"
    __table_args__ = (
        Index("ix_service_categories_code_prefix", "code", postgresql_ops={"code": "text_pattern_ops"}),
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("service_categories.id", ondelete="RESTRICT"), index=True
    )


class CertificationType(Entity, Base):
    __tablename__ = "certification_types"

    code: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    issuer: Mapped[str] = mapped_column(String(120), default="")
    category: Mapped[str] = mapped_column(String(40), default="quality")
    synonyms: Mapped[list[str]] = mapped_column(ARRAY(String(120)), default=list)


class Industry(Entity, Base):
    __tablename__ = "industries"

    code: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    synonyms: Mapped[list[str]] = mapped_column(ARRAY(String(120)), default=list)
