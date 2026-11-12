import uuid
from datetime import datetime
from typing import Literal

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
    department: str | None = None
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
    # SOW 14: the user's department picks their dashboard view ("" clears it).
    department: (
        Literal[
            "",
            "engineering",
            "procurement",
            "planning",
            "project_management",
            "operations",
            "construction",
            "commissioning",
            "qa_qc",
            "hse",
            "supply_chain",
            "management",
            "business_development",
            "marketing",
            "document_control",
        ]
        | None
    ) = None


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
    user_count: int = 0


class RoleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=500)
    permissions: list[str] = Field(min_length=1, max_length=50)


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] | None = Field(default=None, min_length=1, max_length=50)


class PermissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    key: str
    description: str
    assignable: bool = Field(default=False, description="Can be included in an organization's custom role.")
