import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import SessionDep, require
from app.core.exceptions import Conflict, NotFound
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.audit.service import record_audit
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
    description: str = ""


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


# ---------------------------------------------------------------- administration (SOW 4, 24)
# Categories are shared by every organization, so platform curators maintain them (taxonomy:manage). Codes are
# permanent once created; names, synonyms and standards can change. Seeded categories are re-synced by bootstrap.

CanManage = Annotated[Principal, Depends(require(P.TAXONOMY_MANAGE))]


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    parent_code: str | None = Field(default=None, max_length=160)
    key: str = Field(min_length=2, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    synonyms: list[str] = Field(default_factory=list, max_length=30)
    standards: list[str] = Field(default_factory=list, max_length=20)


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    synonyms: list[str] | None = Field(default=None, max_length=30)
    standards: list[str] | None = Field(default=None, max_length=20)


@router.post(
    "/equipment-categories", response_model=EquipmentCategoryRead, status_code=status.HTTP_201_CREATED
)
async def create_equipment_category(
    body: CategoryCreate, principal: CanManage, session: SessionDep
) -> EquipmentCategory:
    """Adds a category (at the top level, or under `parent_code`) without a code change."""
    parent = None
    if body.parent_code:
        parent = await session.scalar(
            select(EquipmentCategory).where(EquipmentCategory.code == body.parent_code)
        )
        if parent is None:
            raise NotFound("Parent category not found")
    code = f"{parent.code}.{body.key}" if parent else body.key
    if await session.scalar(select(EquipmentCategory.id).where(EquipmentCategory.code == code)):
        raise Conflict(f"Category {code!r} already exists", code="duplicate")
    category = EquipmentCategory(
        code=code,
        name=body.name,
        description=body.description,
        level=(parent.level + 1) if parent else 0,
        parent_id=parent.id if parent else None,
        synonyms=[s for s in body.synonyms if s],
        standards=[s for s in body.standards if s],
    )
    session.add(category)
    await session.flush()
    record_audit(session, "taxonomy.category_created", principal=principal, entity_type="equipment_category",
                 entity_id=category.id, changes={"code": code, "name": body.name})  # fmt: skip
    await session.commit()
    await session.refresh(category)
    return category


@router.patch("/equipment-categories/{category_id}", response_model=EquipmentCategoryRead)
async def update_equipment_category(
    category_id: uuid.UUID, body: CategoryUpdate, principal: CanManage, session: SessionDep
) -> EquipmentCategory:
    category = await session.get(EquipmentCategory, category_id)
    if category is None:
        raise NotFound("Category not found")
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(category, key, value)
    record_audit(session, "taxonomy.category_updated", principal=principal, entity_type="equipment_category",
                 entity_id=category.id, changes={k: str(v)[:200] for k, v in changes.items()})  # fmt: skip
    await session.commit()
    await session.refresh(category)
    return category
