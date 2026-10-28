import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, require
from app.core.pagination import CursorPage
from app.domain.enums import UserStatus
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.users.models import Role
from app.modules.users.schemas import (
    PermissionRead,
    RoleRead,
    UserInvite,
    UserRead,
    UserRolesUpdate,
    UserUpdate,
)
from app.modules.users.service import UserService

router = APIRouter(tags=["users"])


def get_user_service(session: SessionDep, principal: PrincipalDep, c: ContainerDep) -> UserService:
    return UserService(session, principal, c.settings, c.email)


Svc = Annotated[UserService, Depends(get_user_service)]
CanRead = Annotated[Principal, Depends(require(P.USER_READ))]
CanManage = Annotated[Principal, Depends(require(P.USER_MANAGE))]


def _role_read(role: Role) -> RoleRead:
    return RoleRead(
        id=role.id,
        key=role.key,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        permissions=sorted(p.key for p in role.permissions),
    )


@router.get("/users", response_model=CursorPage[UserRead])
async def list_users(
    _: CanRead,
    svc: Svc,
    q: Annotated[str | None, Query(max_length=100)] = None,
    status_: Annotated[UserStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    cursor: str | None = None,
) -> CursorPage[UserRead]:
    rows, nxt = await svc.list_page(q=q, status=status_, limit=limit, cursor=cursor)
    return CursorPage(
        items=[UserRead.model_validate(u) for u in rows], next_cursor=nxt, has_more=nxt is not None
    )


@router.post("/users/invite", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def invite_user(body: UserInvite, _: CanManage, svc: Svc) -> UserRead:
    return UserRead.model_validate(await svc.invite(body.email, body.full_name, body.roles))


@router.get("/users/{user_id}", response_model=UserRead)
async def get_user(user_id: uuid.UUID, _: CanRead, svc: Svc) -> UserRead:
    return UserRead.model_validate(await svc.get(user_id))


@router.patch("/users/{user_id}", response_model=UserRead)
async def update_user(user_id: uuid.UUID, body: UserUpdate, svc: Svc) -> UserRead:
    # Self-service for own profile; user:manage for others (checked in the service).
    return UserRead.model_validate(await svc.update(user_id, body.full_name))


@router.post("/users/{user_id}/disable", response_model=UserRead)
async def disable_user(user_id: uuid.UUID, _: CanManage, svc: Svc) -> UserRead:
    return UserRead.model_validate(await svc.set_status(user_id, UserStatus.DISABLED))


@router.post("/users/{user_id}/enable", response_model=UserRead)
async def enable_user(user_id: uuid.UUID, _: CanManage, svc: Svc) -> UserRead:
    return UserRead.model_validate(await svc.set_status(user_id, UserStatus.ACTIVE))


@router.put("/users/{user_id}/roles", response_model=UserRead)
async def set_user_roles(user_id: uuid.UUID, body: UserRolesUpdate, _: CanManage, svc: Svc) -> UserRead:
    return UserRead.model_validate(await svc.set_roles(user_id, body.roles))


@router.get("/roles", response_model=list[RoleRead])
async def list_roles(_: CanRead, svc: Svc) -> list[RoleRead]:
    return [_role_read(r) for r in await svc.list_roles()]


@router.get("/permissions", response_model=list[PermissionRead])
async def list_permissions(_: CanRead, svc: Svc) -> list[PermissionRead]:
    return [PermissionRead.model_validate(p) for p in await svc.list_permissions()]
