import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import JobStatus


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    type: str
    status: JobStatus
    stage: str | None
    progress: int
    input: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    attempts: int
    cost_usd: Decimal
    cancel_requested: bool
    user_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobAccepted(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    events_url: str


class DiagnosticsJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: int = Field(default=3, ge=1, le=10)
    step_seconds: float = Field(default=1.0, ge=0, le=10)
    fail_at_step: int | None = Field(default=None, ge=1, le=10)
