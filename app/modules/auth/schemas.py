import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.domain.enums import TenantStatus


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TenantSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    status: TenantStatus


class CurrentUser(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    tenant: TenantSummary | None
    roles: list[str]
    platform_roles: list[str]
    permissions: list[str]
    impersonating: bool = False


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105
    expires_in: int
    user: CurrentUser


class PasswordForgotRequest(BaseModel):
    email: EmailStr


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=128)


class AcceptInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=128)
