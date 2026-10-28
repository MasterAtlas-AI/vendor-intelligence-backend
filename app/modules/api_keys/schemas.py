import ipaddress
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1, max_length=20)
    expires_at: datetime | None = None
    ip_allowlist: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("expires_at")
    @classmethod
    def _future(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.astimezone(UTC) <= datetime.now(UTC):
            raise ValueError("must be in the future")
        return v

    @field_validator("ip_allowlist")
    @classmethod
    def _networks(cls, v: list[str]) -> list[str]:
        return [str(ipaddress.ip_network(n, strict=False)) for n in v]


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    ip_allowlist: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    created_by: uuid.UUID | None

    @field_validator("ip_allowlist", mode="before")
    @classmethod
    def _stringify(cls, v: list[object]) -> list[str]:
        return [str(n) for n in v]


class ApiKeyCreated(ApiKeyRead):
    key: str = Field(description="The full secret. Shown only once; store it securely.")
