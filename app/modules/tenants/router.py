import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, require
from app.core.pagination import CursorPage
from app.domain.enums import TenantStatus
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.tenants.schemas import (
    TenantAdminRead,
    TenantAdminUpdate,
    TenantCreate,
    TenantRead,
    TenantSelfUpdate,
)
from app.modules.tenants.service import TenantService

router = APIRouter(tags=["tenants"])


def get_tenant_service(session: SessionDep, principal: PrincipalDep, c: ContainerDep) -> TenantService:
    return TenantService(session, principal, c.settings, c.email)


Svc = Annotated[TenantService, Depends(get_tenant_service)]
PlatformAdmin = Annotated[Principal, Depends(require(P.TENANT_MANAGE))]


@router.get("/admin/tenants", response_model=CursorPage[TenantAdminRead])
async def list_tenants(
    _: PlatformAdmin,
    svc: Svc,
    q: Annotated[str | None, Query(max_length=100)] = None,
    status_: Annotated[TenantStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    cursor: str | None = None,
) -> CursorPage[TenantAdminRead]:
    rows, nxt = await svc.list_page(q=q, status=status_, limit=limit, cursor=cursor)
    return CursorPage(
        items=[TenantAdminRead.model_validate(t) for t in rows], next_cursor=nxt, has_more=nxt is not None
    )


@router.post("/admin/tenants", response_model=TenantAdminRead, status_code=status.HTTP_201_CREATED)
async def create_tenant(body: TenantCreate, _: PlatformAdmin, svc: Svc) -> TenantAdminRead:
    return TenantAdminRead.model_validate(await svc.create(body))


@router.get("/admin/tenants/{tenant_id}", response_model=TenantAdminRead)
async def get_tenant(tenant_id: uuid.UUID, _: PlatformAdmin, svc: Svc) -> TenantAdminRead:
    return TenantAdminRead.model_validate(await svc.get(tenant_id))


@router.patch("/admin/tenants/{tenant_id}", response_model=TenantAdminRead)
async def update_tenant(
    tenant_id: uuid.UUID, body: TenantAdminUpdate, _: PlatformAdmin, svc: Svc
) -> TenantAdminRead:
    return TenantAdminRead.model_validate(await svc.update(tenant_id, body))


@router.post("/admin/tenants/{tenant_id}/suspend", response_model=TenantAdminRead)
async def suspend_tenant(tenant_id: uuid.UUID, _: PlatformAdmin, svc: Svc) -> TenantAdminRead:
    return TenantAdminRead.model_validate(await svc.set_status(tenant_id, TenantStatus.SUSPENDED))


@router.post("/admin/tenants/{tenant_id}/activate", response_model=TenantAdminRead)
async def activate_tenant(tenant_id: uuid.UUID, _: PlatformAdmin, svc: Svc) -> TenantAdminRead:
    return TenantAdminRead.model_validate(await svc.set_status(tenant_id, TenantStatus.ACTIVE))


@router.get("/tenant", response_model=TenantRead)
async def get_own_tenant(_: PrincipalDep, svc: Svc) -> TenantRead:
    return TenantRead.model_validate(await svc.get_own())


@router.patch("/tenant", response_model=TenantRead)
async def update_own_tenant(
    body: TenantSelfUpdate, _: Annotated[Principal, Depends(require(P.TENANT_SETTINGS_MANAGE))], svc: Svc
) -> TenantRead:
    return TenantRead.model_validate(await svc.update_own(body))
