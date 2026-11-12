import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field

from app.domain.enums import TenantStatus
from app.modules.tenants.policy import AiControls, controls_from

SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$"


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    status: TenantStatus
    plan: str
    settings: dict[str, Any]
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ai(self) -> AiControls:
        """AI and public-source controls (also inside `settings["ai"]`), with defaults filled in."""
        return controls_from(self.settings)


class TenantAdminRead(TenantRead):
    """Platform view: includes curation and quota fields tenants don't see."""

    trusted_source: bool
    quotas: dict[str, Any]


class TenantAdminInput(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)


class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(pattern=SLUG_PATTERN)
    plan: str = Field(default="starter", max_length=50)
    status: TenantStatus = TenantStatus.TRIAL
    trusted_source: bool = False
    quotas: dict[str, Any] = Field(default_factory=dict)
    admin: TenantAdminInput


class TenantAdminUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=200)
    plan: str | None = Field(default=None, max_length=50)
    trusted_source: bool | None = None
    quotas: dict[str, Any] | None = None


class TenantSelfUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=200)
    settings: dict[str, Any] | None = None
    ai: AiControls | None = None  # merged into settings["ai"]
