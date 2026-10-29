"""Vendor discovery: the request users send, and the structured outputs the AI must return at each step.

AI output schemas are kept simple on purpose (no constraints, no free-form dicts) so they work with strict Structured
Outputs; bounds are enforced after parsing, in code.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.normalization import normalize_country

Region = Literal["global", "europe", "middle_east", "africa", "south_asia", "south_east_asia", "east_asia",
                 "oceania", "north_america", "latin_america"]  # fmt: skip


class DiscoveryRequest(BaseModel):
    """What to look for, never who: categories, industry, region, standards and a free-text brief."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    equipment_categories: list[str] = Field(default_factory=list, max_length=5)  # taxonomy codes
    service_categories: list[str] = Field(default_factory=list, max_length=5)
    industries: list[str] = Field(default_factory=list, max_length=5)  # industry codes
    countries: list[str] = Field(default_factory=list, max_length=20)  # ISO codes or names -> ISO codes
    regions: list[Region] = Field(default_factory=list, max_length=5)
    standards: list[str] = Field(default_factory=list, max_length=10)  # e.g. "API 610", "NORSOK"
    certifications: list[str] = Field(default_factory=list, max_length=10)  # certification codes
    brief: str = Field(default="", max_length=1000)
    max_candidates: int = Field(default=15, ge=3, le=25)
    include_existing: bool = True  # also research vendors already in the knowledge base (new facts only)
    pages_per_vendor: int = Field(default=6, ge=2, le=15)

    @field_validator("countries")
    @classmethod
    def _countries(cls, v: list[str]) -> list[str]:
        codes = []
        for c in v:
            code = normalize_country(c)
            if code is None:
                raise ValueError(f"unknown country {c!r}")
            if code not in codes:
                codes.append(code)
        return codes

    @field_validator("standards")
    @classmethod
    def _standards(cls, v: list[str]) -> list[str]:
        return [s[:40] for s in v if s.strip()]

    @model_validator(mode="after")
    def _something_to_look_for(self) -> "DiscoveryRequest":
        if not (self.equipment_categories or self.service_categories or self.brief.strip()):
            raise ValueError("choose at least one equipment or service category, or describe what you need")
        return self


class DiscoveryEstimate(BaseModel):
    queries: int
    web_searches: int
    candidates: int
    typical_cost_usd: float
    max_cost_usd: float


# ---------------------------------------------------------------- AI outputs


class SearchPlan(BaseModel):
    queries: list[str]


CompanyKind = Literal["manufacturer", "service_provider", "distributor", "directory_or_marketplace",
                      "news_or_media", "other"]  # fmt: skip


class FoundCompany(BaseModel):
    name: str
    website: str | None
    country: str | None
    kind: CompanyKind
    why: str
    relevance: float
    evidence_url: str | None = None  # the company's own page that shows the matching product or service


class SearchFindings(BaseModel):
    companies: list[FoundCompany]


class FactEvidence(BaseModel):
    url: str
    quote: str


class ExtractedEquipment(BaseModel):
    name: str
    category_hint: str
    model_series: str | None
    standards: list[str]
    evidence: list[FactEvidence]
    confidence: float


class ExtractedService(BaseModel):
    name: str
    category_hint: str
    evidence: list[FactEvidence]
    confidence: float


class ExtractedCertification(BaseModel):
    name: str
    certificate_number: str | None
    evidence: list[FactEvidence]
    confidence: float


class ExtractedIndustry(BaseModel):
    name: str
    evidence: list[FactEvidence]
    confidence: float


class ExtractedProject(BaseModel):
    project_name: str
    client: str | None
    country: str | None
    year: int | None
    scope: str
    evidence: list[FactEvidence]
    confidence: float


class VendorExtraction(BaseModel):
    is_relevant: bool
    relevance_reason: str
    legal_name: str
    hq_country: str | None
    hq_city: str | None
    description: str
    equipment: list[ExtractedEquipment]
    services: list[ExtractedService]
    certifications: list[ExtractedCertification]
    industries: list[ExtractedIndustry]
    projects: list[ExtractedProject]


class ClassifiedTerm(BaseModel):
    term: str
    code: str | None


class Classification(BaseModel):
    terms: list[ClassifiedTerm]


class SameCompany(BaseModel):
    relationship: Literal["same", "parent_subsidiary", "distinct", "unsure"]
    confidence: float
    reason: str
