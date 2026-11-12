"""Projects (a tenant's sourcing work: a package, a bid, an FPSO topsides scope), their members, the vendors under
consideration, and scorecards. Everything here is tenant-private (RLS)."""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import Entity, TenantScoped

PROJECT_STATUSES = ("active", "on_hold", "closed", "archived")
# sourcing: a live sourcing workspace (members only); historical: an executed project whose equipment, procurement
# and lessons form the organization's knowledge (readable by everyone with equipment:read).
PROJECT_KINDS = ("sourcing", "historical")
PROJECT_LOCATIONS = ("offshore", "onshore")
MEMBER_ROLES = ("owner", "editor", "viewer")
VENDOR_STAGES = ("longlist", "shortlist", "selected", "rejected")
SCORECARD_STATUSES = ("draft", "final")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Project(Entity, TenantScoped, Base):
    __tablename__ = "tenant_projects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_tenant_projects_tenant_code"),
        CheckConstraint(_in("status", PROJECT_STATUSES), name="status"),
        CheckConstraint(_in("kind", PROJECT_KINDS), name="kind"),
        CheckConstraint(f"location IS NULL OR {_in('location', PROJECT_LOCATIONS)}", name="location"),
    )

    code: Mapped[str | None] = mapped_column(String(40))  # the organization's own project number
    name: Mapped[str] = mapped_column(String(200))
    client: Mapped[str | None] = mapped_column(String(200))
    facility_type: Mapped[str | None] = mapped_column(String(60))  # FPSO, refinery, LNG, ...
    country: Mapped[str | None] = mapped_column(CHAR(2))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    kind: Mapped[str] = mapped_column(String(12), default="sourcing")
    year: Mapped[int | None] = mapped_column(SmallInteger)  # execution / delivery year
    location: Mapped[str | None] = mapped_column(String(10))  # offshore | onshore
    version: Mapped[int] = mapped_column(Integer, default=1)


class ProjectMember(Entity, TenantScoped, Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),
        CheckConstraint(_in("role", MEMBER_ROLES), name="role"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant_projects.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10))


class ProjectVendor(Entity, TenantScoped, Base):
    """A vendor under consideration for a project, and how far it got (longlist -> shortlist -> selected)."""

    __tablename__ = "project_vendors"
    __table_args__ = (
        UniqueConstraint("project_id", "vendor_id", name="uq_project_vendors_project_vendor"),
        CheckConstraint(_in("stage", VENDOR_STAGES), name="stage"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant_projects.id", ondelete="CASCADE"), index=True
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(12), default="longlist")
    note: Mapped[str] = mapped_column(Text, default="")


class ScorecardTemplate(Entity, TenantScoped, Base):
    """Weighted criteria: [{"key": "technical", "label": "Technical fit", "weight": 30, "description": "..."}]."""

    __tablename__ = "scorecard_templates"

    name: Mapped[str] = mapped_column(String(120))
    criteria: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    archived: Mapped[bool] = mapped_column(default=False)


class Scorecard(Entity, TenantScoped, Base):
    """One evaluation of a vendor (optionally for a project). The template's criteria are copied in, so editing the
    template later doesn't change past scores. scores: {"technical": {"score": 8, "comment": "..."}} on a 0-10 scale;
    total_score is the weighted result on 0-100."""

    __tablename__ = "scorecards"
    __table_args__ = (
        CheckConstraint(_in("status", SCORECARD_STATUSES), name="status"),
        Index("ix_scorecards_tenant_vendor", "tenant_id", "vendor_id"),
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenant_projects.id", ondelete="CASCADE"), index=True
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scorecard_templates.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200), default="")
    criteria: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    scores: Mapped[dict[str, Any]] = mapped_column(default=dict)
    total_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    status: Mapped[str] = mapped_column(String(10), default="draft")
    notes: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
