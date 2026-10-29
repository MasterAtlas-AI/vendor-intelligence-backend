import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import SessionDep, require
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.rfqs.schemas import (
    AwardRequest,
    RfqCreate,
    RfqRead,
    RfqStatus,
    RfqUpdate,
    RfqVendorAdd,
    RfqVendorRead,
    RfqVendorUpdate,
    Transition,
)
from app.modules.rfqs.service import RfqService

router = APIRouter(tags=["rfqs"])


def _svc(session: SessionDep, principal: Annotated[Principal, Depends(require(P.VENDOR_READ))]) -> RfqService:
    return RfqService(session, principal)


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
