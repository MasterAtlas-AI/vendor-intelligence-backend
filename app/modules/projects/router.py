import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import SessionDep, require
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.projects.schemas import (
    MemberRead,
    MemberUpsert,
    ProjectCreate,
    ProjectStatus,
    ProjectUpdate,
    ProjectVendorAdd,
    ProjectVendorRead,
    ProjectVendorUpdate,
    ScorecardCreate,
    ScorecardRead,
    ScorecardUpdate,
    TemplateCreate,
    TemplateRead,
    TemplateUpdate,
    TenantProjectRead,
)
from app.modules.projects.service import ProjectService

router = APIRouter(tags=["projects"])


def _svc(
    session: SessionDep, principal: Annotated[Principal, Depends(require(P.VENDOR_READ))]
) -> ProjectService:
    return ProjectService(session, principal)


Svc = Annotated[ProjectService, Depends(_svc)]


# ---------------------------------------------------------------- projects


@router.get("/projects", response_model=list[TenantProjectRead])
async def list_projects(
    svc: Svc, status_: Annotated[ProjectStatus | None, Query(alias="status")] = None
) -> list[TenantProjectRead]:
    """Projects the caller can see (all of the organization's with project:manage, otherwise their own)."""
    return await svc.list_projects(status_)


@router.post("/projects", response_model=TenantProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, svc: Svc) -> TenantProjectRead:
    return await svc.create(body)


@router.get("/projects/{project_id}", response_model=TenantProjectRead)
async def get_project(project_id: uuid.UUID, svc: Svc) -> TenantProjectRead:
    return await svc.get(project_id)


@router.patch("/projects/{project_id}", response_model=TenantProjectRead)
async def update_project(project_id: uuid.UUID, body: ProjectUpdate, svc: Svc) -> TenantProjectRead:
    """Edit details or status (archive with status=archived). Needs the current `version`."""
    return await svc.update(project_id, body)


@router.get("/projects/{project_id}/members", response_model=list[MemberRead])
async def list_members(project_id: uuid.UUID, svc: Svc) -> list[MemberRead]:
    return await svc.members(project_id)


@router.put("/projects/{project_id}/members/{user_id}", response_model=list[MemberRead])
async def set_member(
    project_id: uuid.UUID, user_id: uuid.UUID, body: MemberUpsert, svc: Svc
) -> list[MemberRead]:
    return await svc.set_member(project_id, user_id, body.role)


@router.delete("/projects/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(project_id: uuid.UUID, user_id: uuid.UUID, svc: Svc) -> None:
    await svc.remove_member(project_id, user_id)


@router.get("/projects/{project_id}/vendors", response_model=list[ProjectVendorRead])
async def list_project_vendors(project_id: uuid.UUID, svc: Svc) -> list[ProjectVendorRead]:
    """The project's vendors by stage, ranked by their latest scorecard."""
    return await svc.vendors(project_id)


@router.post("/projects/{project_id}/vendors", response_model=list[ProjectVendorRead])
async def add_project_vendor(
    project_id: uuid.UUID, body: ProjectVendorAdd, svc: Svc
) -> list[ProjectVendorRead]:
    return await svc.add_vendor(project_id, body)


@router.patch("/projects/{project_id}/vendors/{vendor_id}", response_model=list[ProjectVendorRead])
async def update_project_vendor(
    project_id: uuid.UUID, vendor_id: uuid.UUID, body: ProjectVendorUpdate, svc: Svc
) -> list[ProjectVendorRead]:
    return await svc.update_vendor(project_id, vendor_id, body)


@router.delete("/projects/{project_id}/vendors/{vendor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_project_vendor(project_id: uuid.UUID, vendor_id: uuid.UUID, svc: Svc) -> None:
    await svc.remove_vendor(project_id, vendor_id)


@router.get("/vendors/{vendor_id}/projects", response_model=list[TenantProjectRead])
async def vendor_projects(vendor_id: uuid.UUID, svc: Svc) -> list[TenantProjectRead]:
    """The caller's projects this vendor is on."""
    return await svc.vendor_projects(vendor_id)


# ---------------------------------------------------------------- scorecards


@router.get("/scorecard-templates", response_model=list[TemplateRead])
async def list_templates(svc: Svc, include_archived: bool = False) -> list[TemplateRead]:
    return [TemplateRead.model_validate(t) for t in await svc.templates(include_archived)]


@router.post("/scorecard-templates", response_model=TemplateRead, status_code=status.HTTP_201_CREATED)
async def create_template(body: TemplateCreate, svc: Svc) -> TemplateRead:
    return TemplateRead.model_validate(await svc.create_template(body))


@router.patch("/scorecard-templates/{template_id}", response_model=TemplateRead)
async def update_template(template_id: uuid.UUID, body: TemplateUpdate, svc: Svc) -> TemplateRead:
    return TemplateRead.model_validate(await svc.update_template(template_id, body))


@router.get("/scorecards", response_model=list[ScorecardRead])
async def list_scorecards(
    svc: Svc, project_id: uuid.UUID | None = None, vendor_id: uuid.UUID | None = None
) -> list[ScorecardRead]:
    return await svc.scorecards(project_id=project_id, vendor_id=vendor_id)


@router.post("/scorecards", response_model=ScorecardRead, status_code=status.HTTP_201_CREATED)
async def create_scorecard(body: ScorecardCreate, svc: Svc) -> ScorecardRead:
    """A new draft scorecard for a vendor (optionally within a project), from a template (default: the first)."""
    return await svc.create_scorecard(body)


@router.get("/scorecards/{scorecard_id}", response_model=ScorecardRead)
async def get_scorecard(scorecard_id: uuid.UUID, svc: Svc) -> ScorecardRead:
    return await svc.get_scorecard(scorecard_id)


@router.patch("/scorecards/{scorecard_id}", response_model=ScorecardRead)
async def update_scorecard(scorecard_id: uuid.UUID, body: ScorecardUpdate, svc: Svc) -> ScorecardRead:
    """Score criteria (0-10, with comments), edit notes, finalise or reopen. The total is recomputed (0-100)."""
    return await svc.update_scorecard(scorecard_id, body)


@router.delete("/scorecards/{scorecard_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scorecard(scorecard_id: uuid.UUID, svc: Svc) -> None:
    await svc.delete_scorecard(scorecard_id)
