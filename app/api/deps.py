"""FastAPI dependencies: container access, authentication, tenant-bound sessions, permission checks."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.container import Container
from app.core.exceptions import Forbidden, NotFound, Unauthenticated
from app.core.logging import get_logger
from app.db.session import open_session
from app.domain.principal import Principal, TenantContext
from app.modules.auth.service import AuthService
from app.modules.tenants.models import Tenant

log = get_logger(__name__)


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def get_auth_service(container: ContainerDep) -> AuthService:
    return AuthService(container)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_principal(
    request: Request,
    auth: AuthServiceDep,
    container: ContainerDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    x_act_as_tenant: Annotated[uuid.UUID | None, Header()] = None,
) -> Principal:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthenticated("Expected 'Authorization: Bearer <token>'")
        principal = await auth.resolve_bearer(token)
    elif x_api_key:
        principal = await auth.resolve_api_key(x_api_key, request.client.host if request.client else None)
    else:
        raise Unauthenticated(headers={"WWW-Authenticate": "Bearer"})

    if x_act_as_tenant is not None:
        if not principal.is_super_admin:
            raise Forbidden("Only super admins can act as another tenant")
        async with open_session(container.sessionmaker, TenantContext.platform()) as s:
            if await s.get(Tenant, x_act_as_tenant) is None:
                raise NotFound("Tenant not found")
        log.warning("tenant.impersonation", actor_id=str(principal.id), tenant_id=str(x_act_as_tenant))
        principal = replace(principal, tenant_id=x_act_as_tenant, impersonating=True)

    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def get_session(container: ContainerDep, principal: PrincipalDep) -> AsyncIterator[AsyncSession]:
    """Request-scoped session confined to the caller's tenant. Services commit explicitly."""
    async with open_session(container.sessionmaker, principal.tenant_context(), principal.user_id) as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def require(*permissions: str) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory: the caller must hold *all* listed permissions."""

    async def _check(principal: PrincipalDep) -> Principal:
        missing = [p for p in permissions if not principal.has(p)]
        if missing:
            raise Forbidden(f"Missing permission: {', '.join(missing)}")
        return principal

    return _check
