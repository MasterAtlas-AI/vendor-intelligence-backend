"""Typed payloads for each proposal item action, plus the evidence attached to it.

Items are stored as JSONB, but every payload is validated against these models when it is submitted and again
when a curator edits it, so the applier only ever sees well-formed data. Taxonomy references use codes
(stable, human-readable) and are resolved to ids at apply time.
"""

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.exceptions import ValidationFailed
from app.domain.enums import LocationType, ProposalItemAction, SourceKind
from app.domain.normalization import normalize_country, normalize_website, registrable_domain

Str200 = Annotated[str, Field(min_length=1, max_length=200)]
Str300 = Annotated[str, Field(min_length=1, max_length=300)]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _country(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    code = normalize_country(value)
    if code is None:
        raise ValueError(f"unknown country {value!r}; use an ISO 3166 code or English name")
    return code


class CreateVendor(_Payload):
    legal_name: Str300
    display_name: Str200 | None = None
    website: str | None = Field(default=None, max_length=500)
    hq_country: str | None = None
    hq_city: str | None = Field(default=None, max_length=120)
    description: str = Field(default="", max_length=5000)

    _country = field_validator("hq_country")(_country)

    @field_validator("website")
    @classmethod
    def _website(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        normalized = normalize_website(v)
        if normalized is None:
            raise ValueError(f"not a usable website: {v!r}")
        return normalized


VendorField = Literal["legal_name", "display_name", "website", "hq_country", "hq_city", "description"]


class UpdateVendorField(_Payload):
    field: VendorField
    value: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def _normalise(self) -> "UpdateVendorField":
        if self.field == "hq_country":
            self.value = _country(self.value)
        elif self.field == "website" and self.value:
            normalized = normalize_website(self.value)
            if normalized is None:
                raise ValueError(f"not a usable website: {self.value!r}")
            self.value = normalized
        elif self.field in {"legal_name", "display_name"} and not (self.value and self.value.strip()):
            raise ValueError(f"{self.field} can't be empty")
        return self


class AddDomain(_Payload):
    domain: str = Field(min_length=3, max_length=255)

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        domain = registrable_domain(v)
        if domain is None:
            raise ValueError(f"not a registrable domain: {v!r}")
        return domain


class AddAlias(_Payload):
    alias: Str300
    alias_type: Literal["trade", "former", "subsidiary", "abbrev"] = "trade"


class AddLocation(_Payload):
    location_type: LocationType
    address: str = Field(default="", max_length=1000)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = None

    _country = field_validator("country")(_country)


class AddIndustry(_Payload):
    industry_code: str = Field(min_length=1, max_length=60)


class AddEquipment(_Payload):
    category_code: str = Field(min_length=1, max_length=160)
    name: Str300
    model_series: str | None = Field(default=None, max_length=200)
    standards: list[Annotated[str, Field(max_length=80)]] = Field(default_factory=list, max_length=20)
    specs: dict[str, Any] = Field(default_factory=dict)
    applications: list[Annotated[str, Field(max_length=120)]] = Field(default_factory=list, max_length=20)


class AddService(_Payload):
    category_code: str = Field(min_length=1, max_length=160)
    name: Str300
    equipment_category_code: str | None = Field(default=None, max_length=160)
    regions: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=30)


class AddCertification(_Payload):
    certification_code: str = Field(min_length=1, max_length=60)
    certificate_number: str | None = Field(default=None, max_length=120)
    scope: str = Field(default="", max_length=2000)
    valid_from: date | None = None
    valid_to: date | None = None

    @model_validator(mode="after")
    def _dates(self) -> "AddCertification":
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to is before valid_from")
        return self


class AddProject(_Payload):
    project_name: Str300
    client: str | None = Field(default=None, max_length=200)
    operator: str | None = Field(default=None, max_length=200)
    facility_type: str | None = Field(default=None, max_length=60)
    country: str | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    scope: str = Field(default="", max_length=2000)

    _country = field_validator("country")(_country)


PAYLOADS: dict[ProposalItemAction, type[_Payload]] = {
    ProposalItemAction.CREATE_VENDOR: CreateVendor,
    ProposalItemAction.UPDATE_VENDOR_FIELD: UpdateVendorField,
    ProposalItemAction.ADD_DOMAIN: AddDomain,
    ProposalItemAction.ADD_ALIAS: AddAlias,
    ProposalItemAction.ADD_LOCATION: AddLocation,
    ProposalItemAction.ADD_INDUSTRY: AddIndustry,
    ProposalItemAction.ADD_EQUIPMENT: AddEquipment,
    ProposalItemAction.ADD_SERVICE: AddService,
    ProposalItemAction.ADD_CERTIFICATION: AddCertification,
    ProposalItemAction.ADD_PROJECT: AddProject,
}


class Evidence(BaseModel):
    """Where a fact came from. Becomes a vendor_sources row (deduplicated by URL/title) plus a fact_sources link."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: SourceKind = SourceKind.MANUAL
    url: HttpUrl | None = None
    title: str = Field(default="", max_length=300)
    snippet: str = Field(default="", max_length=2000)
    page: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _has_reference(self) -> "Evidence":
        if not self.url and not self.title:
            raise ValueError("evidence needs a url or a title")
        return self


_EVIDENCE_LIST = TypeAdapter(list[Evidence])


def _field_errors(exc: ValidationError, prefix: str) -> list[dict[str, str]]:
    return [
        {"field": ".".join([prefix, *(str(p) for p in err["loc"])]), "message": err["msg"]}
        for err in exc.errors()
    ]


def validate_payload(
    action: ProposalItemAction, payload: dict[str, Any], *, field: str = "payload"
) -> dict[str, Any]:
    """Returns the normalised payload (JSON-safe) or raises ValidationFailed with field-level messages."""
    try:
        return PAYLOADS[action].model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise ValidationFailed(f"Invalid {action.value} item", errors=_field_errors(exc, field)) from exc


def validate_evidence(evidence: list[dict[str, Any]], *, field: str = "evidence") -> list[dict[str, Any]]:
    try:
        return [e.model_dump(mode="json") for e in _EVIDENCE_LIST.validate_python(evidence)]
    except ValidationError as exc:
        raise ValidationFailed("Invalid evidence", errors=_field_errors(exc, field)) from exc
