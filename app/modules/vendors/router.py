import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import PrincipalDep, SessionDep, require
from app.domain.enums import AvlStatus
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.vendors.schemas import TenantOverlayRead, TenantOverlayUpdate, VendorDetail, VendorPage
from app.modules.vendors.service import VendorFilters, VendorQueryService

router = APIRouter(tags=["vendors"])
CanRead = Annotated[Principal, Depends(require(P.VENDOR_READ))]
CanApprove = Annotated[Principal, Depends(require(P.TENANT_VENDOR_APPROVE))]


def get_service(session: SessionDep, principal: PrincipalDep) -> VendorQueryService:
    return VendorQueryService(session, principal)


Svc = Annotated[VendorQueryService, Depends(get_service)]


@router.get("/vendors", response_model=VendorPage)
async def search_vendors(
    _: CanRead,
    svc: Svc,
    q: Annotated[
        str | None, Query(max_length=200, description="Name, alias, website or description text")
    ] = None,
    country: Annotated[
        str | None, Query(max_length=60, description="ISO code or name; HQ or any location")
    ] = None,
    equipment_category: Annotated[
        str | None, Query(max_length=160, description="Taxonomy code, incl. descendants")
    ] = None,
    service_category: Annotated[str | None, Query(max_length=160)] = None,
    certification: Annotated[
        str | None, Query(max_length=60, description="Certification type code, e.g. API_Q1")
    ] = None,
    industry: Annotated[str | None, Query(max_length=60)] = None,
    avl_status: Annotated[AvlStatus | None, Query(description="Your organization's approval status")] = None,
    mine: Annotated[bool, Query(description="Only vendors on your organization's own list")] = False,
    include_inactive: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> VendorPage:
    filters = VendorFilters(
        q=q,
        country=country,
        equipment_category=equipment_category,
        service_category=service_category,
        certification=certification,
        industry=industry,
        avl_status=avl_status,
        mine=mine,
        include_inactive=include_inactive,
    )
    items, nxt = await svc.search(filters, limit=limit, cursor=cursor)
    return VendorPage(items=items, next_cursor=nxt, has_more=nxt is not None)


@router.get("/vendors/{vendor_id}", response_model=VendorDetail)
async def get_vendor(vendor_id: uuid.UUID, _: CanRead, svc: Svc) -> VendorDetail:
    return await svc.detail(vendor_id)


@router.put("/my-vendors/{vendor_id}", response_model=TenantOverlayRead)
async def set_my_vendor(
    vendor_id: uuid.UUID, body: TenantOverlayUpdate, _: CanApprove, svc: Svc
) -> TenantOverlayRead:
    """Add the vendor to your organization's list or update its approval status, internal code, tags or notes."""
    return await svc.set_overlay(vendor_id, body)


@router.delete("/my-vendors/{vendor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_my_vendor(vendor_id: uuid.UUID, _: CanApprove, svc: Svc) -> None:
    await svc.remove_overlay(vendor_id)
