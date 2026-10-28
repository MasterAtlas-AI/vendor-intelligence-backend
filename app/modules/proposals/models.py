"""Knowledge proposals: the only way data enters the global knowledge base.

A proposal is one subject (a new vendor, or an existing vendor) plus N items (create the vendor, add a piece of
equipment, change a field, ...). Curators approve or reject per item; approved items are applied to the canonical
tables in the same transaction. `origin_tenant_id` (who uploaded/suggested it) is tenant-private and is never
shown to other tenants.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, pg_enum
from app.db.mixins import Entity
from app.domain.enums import (
    ApprovalAction,
    ProposalItemAction,
    ProposalItemStatus,
    ProposalKind,
    ProposalOrigin,
    ProposalStatus,
)


class KnowledgeProposal(Entity, Base):
    __tablename__ = "knowledge_proposals"
    __table_args__ = (
        Index("ix_knowledge_proposals_status_created_at", "status", "created_at"),
        Index("ix_knowledge_proposals_origin_tenant_id_created_at", "origin_tenant_id", "created_at"),
    )

    kind: Mapped[ProposalKind] = mapped_column(pg_enum(ProposalKind, "proposal_kind"))
    origin: Mapped[ProposalOrigin] = mapped_column(pg_enum(ProposalOrigin, "proposal_origin"))
    status: Mapped[ProposalStatus] = mapped_column(
        pg_enum(ProposalStatus, "proposal_status"), default=ProposalStatus.PENDING_REVIEW
    )
    title: Mapped[str] = mapped_column(String(300))
    subject_vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    origin_tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"))
    origin_user_id: Mapped[uuid.UUID | None] = mapped_column()
    origin_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    assigned_to: Mapped[uuid.UUID | None] = mapped_column()
    decided_by: Mapped[uuid.UUID | None] = mapped_column()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)

    items: Mapped[list["ProposalItem"]] = relationship(
        back_populates="proposal",
        lazy="selectin",
        order_by="ProposalItem.sort_order",
        cascade="all, delete-orphan",
    )


class ProposalItem(Entity, Base):
    __tablename__ = "proposal_items"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_proposals.id", ondelete="CASCADE"), index=True
    )
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)
    action: Mapped[ProposalItemAction] = mapped_column(pg_enum(ProposalItemAction, "proposal_item_action"))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    status: Mapped[ProposalItemStatus] = mapped_column(
        pg_enum(ProposalItemStatus, "proposal_item_status"), default=ProposalItemStatus.PENDING
    )
    applied_table: Mapped[str | None] = mapped_column(String(40))
    applied_entity_id: Mapped[uuid.UUID | None] = mapped_column()
    decided_by: Mapped[uuid.UUID | None] = mapped_column()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str] = mapped_column(Text, default="")

    proposal: Mapped[KnowledgeProposal] = relationship(back_populates="items")


class ApprovalHistory(Entity, Base):
    """Every curator decision and edit, with before/after, for audit and for tuning future extraction."""

    __tablename__ = "approval_history"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_proposals.id", ondelete="CASCADE"), index=True
    )
    proposal_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proposal_items.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[ApprovalAction] = mapped_column(pg_enum(ApprovalAction, "approval_action"))
    reason: Mapped[str] = mapped_column(Text, default="")
    before: Mapped[dict[str, Any] | None] = mapped_column()
    after: Mapped[dict[str, Any] | None] = mapped_column()
    actor_id: Mapped[uuid.UUID | None] = mapped_column()
