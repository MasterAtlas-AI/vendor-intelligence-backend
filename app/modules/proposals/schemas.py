import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    ApprovalAction,
    ProposalItemAction,
    ProposalItemStatus,
    ProposalKind,
    ProposalOrigin,
    ProposalStatus,
)
from app.modules.vendors.schemas import VendorDetail


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ProposalItemAction
    payload: dict[str, Any]
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    confidence: Decimal | None = Field(default=None, ge=0, le=1)


class ProposalCreate(BaseModel):
    """A curator entering knowledge by hand (origin = manual)."""

    model_config = ConfigDict(extra="forbid")
    kind: ProposalKind
    subject_vendor_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=300)
    items: list[ItemIn] = Field(min_length=1, max_length=200)


class ItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] | None = Field(default=None, max_length=20)
    version: int


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[uuid.UUID] | None = Field(
        default=None, description="Items to decide; omit to decide every pending item"
    )
    note: str = Field(default="", max_length=2000)
    version: int = Field(description="The proposal version you reviewed (optimistic locking)")


class RequestInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=2000)
    version: int


class ItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sort_order: int
    action: ProposalItemAction
    payload: dict[str, Any]
    evidence: list[dict[str, Any]]
    confidence: Decimal | None
    status: ProposalItemStatus
    applied_table: str | None
    applied_entity_id: uuid.UUID | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    reject_reason: str


class ItemCounts(BaseModel):
    pending: int = 0
    applied: int = 0
    rejected: int = 0


class BulkTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    version: int = Field(description="The version you saw in the list (optimistic locking)")


class BulkDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject"]
    proposals: list[BulkTarget] = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=2000, description="Required when rejecting")


class BulkOutcome(BaseModel):
    id: uuid.UUID
    ok: bool
    status: ProposalStatus | None = None
    error: str | None = None


class BulkResult(BaseModel):
    done: int
    failed: int
    results: list[BulkOutcome]


class ProposalSummary(BaseModel):
    id: uuid.UUID
    kind: ProposalKind
    origin: ProposalOrigin
    status: ProposalStatus
    title: str
    subject_vendor_id: uuid.UUID | None
    confidence: Decimal | None
    counts: ItemCounts
    created_at: datetime
    decided_at: datetime | None
    version: int


class HistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    proposal_item_id: uuid.UUID | None
    action: ApprovalAction
    reason: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    actor_id: uuid.UUID | None
    created_at: datetime


class ProposalDetail(ProposalSummary):
    """Curator view. `origin_tenant_id` is platform-only information."""

    origin_tenant_id: uuid.UUID | None
    origin_job_id: uuid.UUID | None
    decision_note: str
    items: list[ItemRead]
    history: list[HistoryRead]
    subject_vendor: VendorDetail | None


class ProposalPage(BaseModel):
    items: list[ProposalSummary]
    next_cursor: str | None
    has_more: bool
