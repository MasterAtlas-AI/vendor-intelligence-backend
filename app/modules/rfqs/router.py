import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import ContainerDep, SessionDep, require
from app.db.session import open_session
from app.domain.permissions import P
from app.domain.principal import Principal, TenantContext
from app.modules.rfqs.schemas import (
    AwardRequest,
    EmailPreview,
    EmailSend,
    EmailSendResult,
    RfqCreate,
    RfqRead,
    RfqStatus,
    RfqUpdate,
    RfqVendorAdd,
    RfqVendorRead,
    RfqVendorUpdate,
    Transition,
)
from app.modules.rfqs.service import RfqService, Sender
from app.modules.users.models import User

router = APIRouter(tags=["rfqs"])


def _svc(
    session: SessionDep, principal: Annotated[Principal, Depends(require(P.VENDOR_READ))], c: ContainerDep
) -> RfqService:
    return RfqService(session, principal, c.settings.rfq_test_recipient)


Svc = Annotated[RfqService, Depends(_svc)]


@router.get("/rfqs", response_model=list[RfqRead])
async def list_rfqs(
    svc: Svc,
    status_: Annotated[RfqStatus | None, Query(alias="status")] = None,
    project_id: uuid.UUID | None = None,
) -> list[RfqRead]:
    return await svc.list_rfqs(status=status_, project_id=project_id)


@router.post("/rfqs", response_model=RfqRead, status_code=status.HTTP_201_CREATED)
async def create_rfq(body: RfqCreate, svc: Svc) -> RfqRead:
    """A draft RFQ; with `project_id` and `from_shortlist`, the project's shortlisted and selected vendors join it."""
    return await svc.create(body)


@router.get("/rfqs/{rfq_id}", response_model=RfqRead)
async def get_rfq(rfq_id: uuid.UUID, svc: Svc) -> RfqRead:
    return await svc.get(rfq_id)


@router.patch("/rfqs/{rfq_id}", response_model=RfqRead)
async def update_rfq(rfq_id: uuid.UUID, body: RfqUpdate, svc: Svc) -> RfqRead:
    return await svc.update(rfq_id, body)


def _transition_route(target: str, path: str, summary: str) -> None:
    async def move(rfq_id: uuid.UUID, body: Transition, svc: Svc) -> RfqRead:
        return await svc.transition(rfq_id, target, body)

    move.__name__ = f"rfq_{path}"
    router.add_api_route(
        f"/rfqs/{{rfq_id}}/{path}", move, methods=["POST"], response_model=RfqRead, summary=summary
    )


_transition_route("issued", "issue", "Issue the RFQ (shortlisted vendors become invited)")
_transition_route("evaluating", "evaluate", "Start evaluating the quotes")
_transition_route("closed", "close", "Close an awarded RFQ")
_transition_route("cancelled", "cancel", "Cancel the RFQ")


@router.post("/rfqs/{rfq_id}/award", response_model=RfqRead)
async def award_rfq(rfq_id: uuid.UUID, body: AwardRequest, svc: Svc) -> RfqRead:
    """Award to a vendor that quoted; the other quoted vendors are marked lost."""
    return await svc.award(rfq_id, body)


async def _sender(c: ContainerDep, principal: Principal) -> Sender:
    """The signed-in user's own name and address, read with platform rights: when platform staff view as an
    organization, their user row is outside that organization's data."""
    if principal.user_id is None:
        return Sender(email=None, name=None)
    async with open_session(c.sessionmaker, TenantContext.platform()) as s:
        user = await s.get(User, principal.user_id)
    return Sender(email=user.email, name=user.full_name) if user else Sender(email=None, name=None)


@router.get("/rfqs/{rfq_id}/email", response_model=EmailPreview)
async def rfq_email_preview(rfq_id: uuid.UUID, svc: Svc, c: ContainerDep) -> EmailPreview:
    """The e-mail each vendor would receive (subject, text with the requirements) and who has an address."""
    return await svc.email_preview(rfq_id, c.settings, await _sender(c, svc.principal))


@router.post("/rfqs/{rfq_id}/email", response_model=EmailSendResult)
async def send_rfq_email(rfq_id: uuid.UUID, body: EmailSend, svc: Svc, c: ContainerDep) -> EmailSendResult:
    """E-mails the RFQ to the chosen vendors, one message each (no vendor sees another), replies going to the
    sender. A draft RFQ is issued by it. `test_to_me` sends a single copy to the sender instead."""
    return await svc.send_email(rfq_id, body, c.settings, c.email, await _sender(c, svc.principal))


@router.get("/rfqs/{rfq_id}/vendors", response_model=list[RfqVendorRead])
async def list_rfq_vendors(rfq_id: uuid.UUID, svc: Svc) -> list[RfqVendorRead]:
    """Vendors with their quotes, lead times and project scorecard scores, best first."""
    return await svc.vendors(rfq_id)


@router.post("/rfqs/{rfq_id}/vendors", response_model=list[RfqVendorRead])
async def add_rfq_vendor(rfq_id: uuid.UUID, body: RfqVendorAdd, svc: Svc) -> list[RfqVendorRead]:
    return await svc.add_vendor(rfq_id, body.vendor_id)


@router.patch("/rfqs/{rfq_id}/vendors/{vendor_id}", response_model=list[RfqVendorRead])
async def update_rfq_vendor(
    rfq_id: uuid.UUID, vendor_id: uuid.UUID, body: RfqVendorUpdate, svc: Svc
) -> list[RfqVendorRead]:
    """Record a quote (amount, currency, lead time), an invitation or a decline."""
    return await svc.update_vendor(rfq_id, vendor_id, body)


@router.delete("/rfqs/{rfq_id}/vendors/{vendor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_rfq_vendor(rfq_id: uuid.UUID, vendor_id: uuid.UUID, svc: Svc) -> None:
    await svc.remove_vendor(rfq_id, vendor_id)
