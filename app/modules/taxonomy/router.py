import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import SessionDep, require
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.taxonomy.models import CertificationType, EquipmentCategory, Industry, ServiceCategory

router = APIRouter(prefix="/taxonomy", tags=["taxonomy"])
CanRead = Annotated[Principal, Depends(require(P.VENDOR_READ))]


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    parent_id: uuid.UUID | None
    code: str
    name: str
    level: int
    synonyms: list[str]


class EquipmentCategoryRead(CategoryRead):
    standards: list[str]


class CertificationTypeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str
    issuer: str
    category: str


class IndustryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str
    synonyms: list[str]


# Flat lists in tree order (parents before children); clients build the tree from parent_id. A few hundred
# rows at most, so no pagination.


@router.get("/equipment-categories", response_model=list[EquipmentCategoryRead])
async def equipment_categories(_: CanRead, session: SessionDep) -> list[EquipmentCategory]:
    return list(await session.scalars(select(EquipmentCategory).order_by(EquipmentCategory.code)))


@router.get("/service-categories", response_model=list[CategoryRead])
async def service_categories(_: CanRead, session: SessionDep) -> list[ServiceCategory]:
    return list(await session.scalars(select(ServiceCategory).order_by(ServiceCategory.code)))


@router.get("/certification-types", response_model=list[CertificationTypeRead])
async def certification_types(_: CanRead, session: SessionDep) -> list[CertificationType]:
    return list(await session.scalars(select(CertificationType).order_by(CertificationType.name)))


@router.get("/industries", response_model=list[IndustryRead])
async def industries(_: CanRead, session: SessionDep) -> list[Industry]:
    return list(await session.scalars(select(Industry).order_by(Industry.name)))
