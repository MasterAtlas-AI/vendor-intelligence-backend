import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.normalization import normalize_country
from app.modules.vendors.schemas import VendorSummary

ProjectStatus = Literal["active", "on_hold", "closed", "archived"]
MemberRole = Literal["owner", "editor", "viewer"]
VendorStage = Literal["longlist", "shortlist", "selected", "rejected"]
ScorecardStatus = Literal["draft", "final"]
# sourcing: a live sourcing workspace (members only); historical: an executed project, part of the organization's
# knowledge (its equipment, procurement and lessons are readable by everyone with equipment:read).
ProjectKind = Literal["sourcing", "historical"]
ProjectLocation = Literal["offshore", "onshore"]


class _Country(BaseModel):
    @field_validator("country", check_fields=False)
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        code = normalize_country(v)
        if code is None:
            raise ValueError(f"unknown country {v!r}")
        return code


class ProjectCreate(_Country):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=40)
    client: str | None = Field(default=None, max_length=200)
    facility_type: str | None = Field(default=None, max_length=60)
    country: str | None = None
    description: str = Field(default="", max_length=5000)
    kind: ProjectKind = "sourcing"
    year: int | None = Field(default=None, ge=1900, le=2100)
    location: ProjectLocation | None = None


class ProjectUpdate(_Country):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: int
    name: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=40)
    client: str | None = Field(default=None, max_length=200)
    facility_type: str | None = Field(default=None, max_length=60)
    country: str | None = None
    description: str | None = Field(default=None, max_length=5000)
    status: ProjectStatus | None = None
    kind: ProjectKind | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    location: ProjectLocation | None = None


class TenantProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str | None
    name: str
    client: str | None
    facility_type: str | None
    country: str | None
    description: str
    status: ProjectStatus
    kind: ProjectKind = "sourcing"
    year: int | None = None
    location: ProjectLocation | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    my_role: MemberRole | None = None  # the caller's membership (None: sees it as a project manager)
    can_edit: bool = False
    vendor_counts: dict[str, int] = Field(default_factory=dict)  # by stage


class MemberRead(BaseModel):
    user_id: uuid.UUID
    full_name: str
    email: str
    role: MemberRole


class MemberUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: MemberRole


class ProjectVendorAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_id: uuid.UUID
    stage: VendorStage = "longlist"
    note: str = Field(default="", max_length=2000)


class ProjectVendorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: VendorStage | None = None
    note: str | None = Field(default=None, max_length=2000)


class ScoreSummary(BaseModel):
    scorecard_id: uuid.UUID
    total_score: Decimal | None
    status: ScorecardStatus
    updated_at: datetime


class ProjectVendorRead(BaseModel):
    vendor: VendorSummary
    stage: VendorStage
    note: str
    added_at: datetime
    scorecard: ScoreSummary | None = None  # the latest scorecard for this vendor in this project
    rank: int | None = None  # by score, among scored vendors


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=120)
    weight: float = Field(gt=0, le=100)
    description: str = Field(default="", max_length=500)


def _check_criteria(criteria: list[Criterion]) -> list[Criterion]:
    if len({c.key for c in criteria}) != len(criteria):
        raise ValueError("criterion keys must be unique")
    return criteria


class TemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    criteria: list[Criterion] = Field(min_length=1, max_length=30)

    @field_validator("criteria")
    @classmethod
    def _unique(cls, v: list[Criterion]) -> list[Criterion]:
        return _check_criteria(v)


class TemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    criteria: list[Criterion] | None = Field(default=None, min_length=1, max_length=30)
    archived: bool | None = None

    @field_validator("criteria")
    @classmethod
    def _unique(cls, v: list[Criterion] | None) -> list[Criterion] | None:
        return None if v is None else _check_criteria(v)


class TemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    criteria: list[Criterion]
    archived: bool
    updated_at: datetime


class ScorecardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_id: uuid.UUID
    project_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None  # default: the organization's first template
    title: str = Field(default="", max_length=200)


class CriterionScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: float | None = Field(default=None, ge=0, le=10)
    comment: str = Field(default="", max_length=2000)


class ScorecardUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    scores: dict[str, CriterionScore] | None = None
    title: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    status: ScorecardStatus | None = None

    @model_validator(mode="after")
    def _something(self) -> "ScorecardUpdate":
        if self.scores is None and self.title is None and self.notes is None and self.status is None:
            raise ValueError("nothing to update")
        return self


class ScorecardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    vendor_id: uuid.UUID
    vendor_name: str = ""
    project_id: uuid.UUID | None
    project_name: str | None = None
    template_id: uuid.UUID | None
    title: str
    criteria: list[Criterion]
    scores: dict[str, CriterionScore]
    total_score: Decimal | None
    status: ScorecardStatus
    notes: str
    version: int
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
