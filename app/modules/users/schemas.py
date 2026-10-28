import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.domain.enums import UserStatus


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    full_name: str
    status: UserStatus
    roles: list[str] = Field(validation_alias="role_keys")
    email_verified: bool
    last_login_at: datetime | None
    created_at: datetime


class UserInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    roles: list[str] = Field(min_length=1, max_length=10)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str | None = Field(default=None, min_length=1, max_length=200)


class UserRolesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[str] = Field(min_length=1, max_length=10)


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    key: str
    name: str
    description: str
    is_system: bool
    permissions: list[str]


class PermissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    key: str
    description: str
