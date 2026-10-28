import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import ContainerDep, PrincipalDep, SessionDep, require
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.api_keys.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyRead
from app.modules.api_keys.service import ApiKeyService

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


def get_api_key_service(session: SessionDep, principal: PrincipalDep, c: ContainerDep) -> ApiKeyService:
    return ApiKeyService(session, principal, c.settings)


Svc = Annotated[ApiKeyService, Depends(get_api_key_service)]
CanManage = Annotated[Principal, Depends(require(P.APIKEY_MANAGE))]


@router.get("", response_model=list[ApiKeyRead])
async def list_api_keys(_: CanManage, svc: Svc) -> list[ApiKeyRead]:
    return [ApiKeyRead.model_validate(k) for k in await svc.list_keys()]


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(body: ApiKeyCreate, _: CanManage, svc: Svc) -> ApiKeyCreated:
    key, secret = await svc.create(body)
    return ApiKeyCreated.model_validate({**ApiKeyRead.model_validate(key).model_dump(), "key": secret})


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(key_id: uuid.UUID, _: CanManage, svc: Svc) -> None:
    await svc.revoke(key_id)
