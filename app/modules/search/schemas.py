import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import AvlStatus
from app.modules.vendors.schemas import VendorSummary


class SearchIntent(BaseModel):
    """What a query asks for, as taxonomy codes plus leftover keywords. Shown to the user as editable chips; also the
    AI's output schema for query understanding (kept flat and unconstrained for strict Structured Outputs)."""

    equipment_categories: list[str] = Field(default_factory=list)
    service_categories: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    standards: list[str] = Field(default_factory=list)  # "API 610", "NORSOK M-650"
    countries: list[str] = Field(default_factory=list)  # ISO codes
    keywords: str = ""  # what's left for full-text and semantic matching

    @property
    def structured(self) -> bool:
        return bool(
            self.equipment_categories or self.service_categories or self.industries or self.certifications
        )


class SearchFilters(BaseModel):
    """Hard filters (results must match), unlike the intent, which ranks."""

    model_config = ConfigDict(extra="forbid")
    countries: list[str] = Field(default_factory=list, max_length=20)
    avl_status: list[AvlStatus] = Field(default_factory=list, max_length=5)
    my_vendors_only: bool = False


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=500)
    # The chips as the user edited them. When given, the query isn't parsed again.
    intent: SearchIntent | None = None
    parser: Literal["local", "ai"] = "local"  # local: instant and free; ai: better with free-form wording
    filters: SearchFilters = Field(default_factory=SearchFilters)
    limit: int = Field(default=20, ge=1, le=50)


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    parser: Literal["local", "ai"] = "ai"


class ParseResult(BaseModel):
    intent: SearchIntent
    parser: Literal["local", "ai"]
    cached: bool = False


class SourceRef(BaseModel):
    url: str | None
    title: str = ""
    snippet: str = ""


class MatchReason(BaseModel):
    """One reason a vendor matched, with the knowledge-base fact and where it came from."""

    kind: Literal["equipment", "service", "certification", "industry", "standard", "text", "semantic"]
    label: str  # "API 610 BB2 pump: HX-2"
    code: str | None = None
    sources: list[SourceRef] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    equipment: float | None = None  # None: the query didn't ask about it (its weight is shared out)
    standards_certifications: float | None = None
    industries: float | None = None
    services: float | None = None
    semantic: float | None = None  # None: no vectors available (AI not configured or nothing indexed)
    text: float | None = None  # name/description words
    data_quality: float


class SearchHit(BaseModel):
    vendor: VendorSummary
    confidence: float  # 0..1
    scores: ScoreBreakdown
    reasons: list[MatchReason]


class SearchResponse(BaseModel):
    search_id: uuid.UUID | None
    intent: SearchIntent
    parser: Literal["local", "ai"]
    semantic: bool  # vector similarity was used (AI configured and the index has vectors)
    took_ms: int
    results: list[SearchHit]


class SearchHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    query: str
    intent: SearchIntent
    filters: SearchFilters
    result_count: int
    took_ms: int
    created_at: datetime


class Suggestion(BaseModel):
    kind: Literal["vendor", "equipment", "service", "certification", "industry"]
    label: str
    value: str  # vendor id or taxonomy code
