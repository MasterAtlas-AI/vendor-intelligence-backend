import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.api.deps import ContainerDep, SessionDep, require
from app.core.exceptions import Forbidden, NotFound
from app.db.session import open_session
from app.domain.permissions import P
from app.domain.principal import Principal, TenantContext
from app.modules.webhooks.models import EVENTS, WebhookDelivery, WebhookEndpoint
from app.modules.webhooks.service import new_secret, queue_delivery

router = APIRouter(tags=["webhooks"])

Event = Literal["vendor.updated", "proposal.approved", "job.completed"]


class EndpointCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: str = Field(min_length=10, max_length=500)
    events: list[Event] = Field(min_length=1, max_length=len(EVENTS))
    description: str = Field(default="", max_length=200)

    @field_validator("url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.lower().startswith("https://"):
            raise ValueError("webhook URLs must use https://")
        return v


class EndpointUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: str | None = Field(default=None, min_length=10, max_length=500)
    events: list[Event] | None = Field(default=None, min_length=1, max_length=len(EVENTS))
    description: str | None = Field(default=None, max_length=200)
    active: bool | None = None

    @field_validator("url")
    @classmethod
    def _https(cls, v: str | None) -> str | None:
        if v is not None and not v.lower().startswith("https://"):
            raise ValueError("webhook URLs must use https://")
        return v


class EndpointRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    url: str
    events: list[str]
    description: str
    active: bool
    failure_count: int
    disabled_reason: str | None
    last_delivery_at: datetime | None
    last_status: int | None
    created_at: datetime


class EndpointWithSecret(EndpointRead):
    secret: str = Field(description="Signs every delivery (HMAC-SHA256). Shown only now; store it securely.")


class DeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    event: str
    status: str
    attempts: int
    response_status: int | None
    error: str | None
    next_attempt_at: datetime
    delivered_at: datetime | None
    created_at: datetime
    payload: dict[str, Any]


Admin = Annotated[Principal, Depends(require(P.APIKEY_MANAGE))]


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise Forbidden("Webhooks belong to an organization")
    return principal.tenant_id


async def _endpoint(session: SessionDep, endpoint_id: uuid.UUID) -> WebhookEndpoint:
    ep = await session.get(WebhookEndpoint, endpoint_id)
    if ep is None:
        raise NotFound("Webhook not found")
    return ep


@router.get("/webhooks", response_model=list[EndpointRead])
async def list_webhooks(principal: Admin, session: SessionDep) -> list[WebhookEndpoint]:
    _tenant(principal)
    return list(await session.scalars(select(WebhookEndpoint).order_by(WebhookEndpoint.created_at)))


@router.post("/webhooks", response_model=EndpointWithSecret, status_code=status.HTTP_201_CREATED)
async def create_webhook(body: EndpointCreate, principal: Admin, session: SessionDep) -> WebhookEndpoint:
    """Register an HTTPS endpoint for events. The signing secret is returned once."""
    ep = WebhookEndpoint(
        tenant_id=_tenant(principal),
        url=body.url,
        events=list(dict.fromkeys(body.events)),
        description=body.description,
        secret=new_secret(),
    )
    session.add(ep)
    await session.commit()
    await session.refresh(ep)
    return ep


@router.patch("/webhooks/{endpoint_id}", response_model=EndpointRead)
async def update_webhook(
    endpoint_id: uuid.UUID, body: EndpointUpdate, principal: Admin, session: SessionDep
) -> Any:
    _tenant(principal)
    ep = await _endpoint(session, endpoint_id)
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(ep, key, value)
    if changes.get("active"):
        ep.failure_count, ep.disabled_reason = 0, None  # switched back on: start counting afresh
    await session.commit()
    await session.refresh(ep)
    return ep


@router.post("/webhooks/{endpoint_id}/rotate-secret", response_model=EndpointWithSecret)
async def rotate_secret(endpoint_id: uuid.UUID, principal: Admin, session: SessionDep) -> Any:
    _tenant(principal)
    ep = await _endpoint(session, endpoint_id)
    ep.secret = new_secret()
    await session.commit()
    await session.refresh(ep)
    return ep


@router.delete("/webhooks/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(endpoint_id: uuid.UUID, principal: Admin, session: SessionDep) -> None:
    _tenant(principal)
    await session.delete(await _endpoint(session, endpoint_id))
    await session.commit()


@router.post(
    "/webhooks/{endpoint_id}/test", response_model=DeliveryRead, status_code=status.HTTP_202_ACCEPTED
)
async def test_webhook(endpoint_id: uuid.UUID, principal: Admin, session: SessionDep, c: ContainerDep) -> Any:
    """Send a `ping` event to this endpoint only."""
    tenant_id = _tenant(principal)
    ep = await _endpoint(session, endpoint_id)
    now = datetime.now(UTC)
    d = WebhookDelivery(
        tenant_id=tenant_id,
        endpoint_id=ep.id,
        event="ping",
        next_attempt_at=now,
        payload={"event": "ping", "created_at": now.isoformat(), "data": {"webhook_id": str(ep.id)}},
    )
    session.add(d)
    await session.commit()
    await session.refresh(d)
    # The delivery job belongs to no organization, so the tenant session may not insert it.
    async with open_session(c.sessionmaker, TenantContext.platform()) as s:
        await queue_delivery(s)
        await s.commit()
    c.job_runner.notify()
    return d


@router.get("/webhooks/{endpoint_id}/deliveries", response_model=list[DeliveryRead])
async def list_deliveries(
    endpoint_id: uuid.UUID,
    principal: Admin,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[WebhookDelivery]:
    _tenant(principal)
    await _endpoint(session, endpoint_id)
    return list(
        await session.scalars(
            select(WebhookDelivery)
            .where(WebhookDelivery.endpoint_id == endpoint_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
        )
    )
