import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, require
from app.core.pagination import CursorPage
from app.domain.enums import UserStatus
from app.domain.permissions import TENANT_ASSIGNABLE, P
from app.domain.principal import Principal
from app.modules.users.models import Role
from app.modules.users.schemas import (
    PermissionRead,
    RoleCreate,
    RoleRead,
    RoleUpdate,
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
CanManageRoles = Annotated[Principal, Depends(require(P.ROLE_MANAGE))]


def _role_read(role: Role, user_count: int = 0) -> RoleRead:
    return RoleRead(
        id=role.id,
        key=role.key,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        permissions=sorted(p.key for p in role.permissions),
        user_count=user_count,
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
    return UserRead.model_validate(await svc.update(user_id, body.full_name, body.department))


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
    return [_role_read(r, n) for r, n in await svc.list_roles()]


@router.post("/roles", response_model=RoleRead, status_code=status.HTTP_201_CREATED)
async def create_role(body: RoleCreate, _: CanManageRoles, svc: Svc) -> RoleRead:
    """A custom role for this organization, built from the permission catalogue (SOW 20)."""
    return _role_read(await svc.create_role(body.name, body.description, body.permissions))


@router.patch("/roles/{role_id}", response_model=RoleRead)
async def update_role(role_id: uuid.UUID, body: RoleUpdate, _: CanManageRoles, svc: Svc) -> RoleRead:
    role = await svc.update_role(
        role_id, name=body.name, description=body.description, permissions=body.permissions
    )
    return _role_read(role)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(role_id: uuid.UUID, _: CanManageRoles, svc: Svc) -> None:
    await svc.delete_role(role_id)


@router.get("/permissions", response_model=list[PermissionRead])
async def list_permissions(_: CanRead, svc: Svc) -> list[PermissionRead]:
    return [
        PermissionRead(key=p.key, description=p.description, assignable=p.key in TENANT_ASSIGNABLE)
        for p in await svc.list_permissions()
    ]
