"""Projects, their members and vendors, scorecard templates and scorecards.

Access, on top of RLS (which keeps everything inside the organization):
- `project:manage` (tenant admins, managers) sees and edits every project of the organization and creates projects;
- everyone else sees the projects they are a member of: owners and editors edit them (details, vendors), viewers read;
- owners (and project managers) manage the member list;
- scorecards need `scorecard:manage` to create or edit, and project edit rights when attached to a project;
- templates are managed with `project:manage` and readable by everyone.
"""

import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, Forbidden, NotFound, ValidationFailed, VersionConflict
from app.domain.enums import UserStatus
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.audit.service import record_audit
from app.modules.projects.models import Project, ProjectMember, ProjectVendor, Scorecard, ScorecardTemplate
from app.modules.projects.schemas import (
    MemberRead,
    ProjectCreate,
    ProjectUpdate,
    ProjectVendorAdd,
    ProjectVendorRead,
    ProjectVendorUpdate,
    ScorecardCreate,
    ScorecardRead,
    ScorecardUpdate,
    ScoreSummary,
    TemplateCreate,
    TemplateUpdate,
    TenantProjectRead,
)
from app.modules.users.models import User
from app.modules.vendors.models import Vendor
from app.modules.vendors.service import VendorFilters, VendorQueryService

DEFAULT_TEMPLATE = {
    "name": "Technical & commercial",
    "criteria": [
        {
            "key": "technical",
            "label": "Technical fit",
            "weight": 30,
            "description": "Meets the specification, standards and duty",
        },
        {
            "key": "quality",
            "label": "Quality & certifications",
            "weight": 20,
            "description": "QA system, certifications, track record",
        },
        {"key": "commercial", "label": "Price & terms", "weight": 20, "description": ""},
        {
            "key": "delivery",
            "label": "Delivery & capacity",
            "weight": 15,
            "description": "Lead time, workload",
        },
        {"key": "hse", "label": "HSE", "weight": 10, "description": "Safety and environmental performance"},
        {"key": "after_sales", "label": "After-sales & local support", "weight": 5, "description": ""},
    ],
}


def weighted_total(criteria: list[dict[str, Any]], scores: dict[str, Any]) -> Decimal | None:
    """0-100 from 0-10 scores, over the criteria that have a score (None until at least one is scored)."""
    scored = [
        (float(c["weight"]), float(scores[c["key"]]["score"]))
        for c in criteria
        if scores.get(c["key"], {}).get("score") is not None
    ]
    if not scored:
        return None
    total = sum(w * s for w, s in scored) / sum(w for w, _ in scored) * 10
    return Decimal(str(total)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class ProjectService:
    def __init__(self, session: AsyncSession, principal: Principal) -> None:
        if principal.tenant_id is None:
            raise Forbidden("Projects belong to an organization; open the app as one to use them")
        self.s = session
        self.principal = principal
        self.tenant_id: uuid.UUID = principal.tenant_id

    @property
    def manager(self) -> bool:
        return self.principal.has(P.PROJECT_MANAGE)

    # ------------------------------------------------------------------ access

    async def _role(self, project_id: uuid.UUID) -> str | None:
        if self.principal.user_id is None:
            return None
        return await self.s.scalar(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == self.principal.user_id
            )
        )

    async def _project(self, project_id: uuid.UUID, *, edit: bool = False, owner: bool = False) -> Project:
        project = await self.s.get(Project, project_id)
        if project is None:
            raise NotFound("Project not found")
        role = await self._role(project_id)
        if not self.manager and role is None:
            raise NotFound("Project not found")
        if owner and not (self.manager or role == "owner"):
            raise Forbidden("Only the project's owners can manage its members")
        if edit and not (self.manager or role in ("owner", "editor")):
            raise Forbidden("You can view this project but not change it")
        return project

    async def _read(self, project: Project) -> TenantProjectRead:
        role = await self._role(project.id)
        counts = dict(
            (
                await self.s.execute(
                    select(ProjectVendor.stage, func.count())
                    .where(ProjectVendor.project_id == project.id)
                    .group_by(ProjectVendor.stage)
                )
            ).all()
        )
        out = TenantProjectRead.model_validate(project)
        out.my_role = role  # type: ignore[assignment]
        out.can_edit = self.manager or role in ("owner", "editor")
        out.vendor_counts = {k: int(v) for k, v in counts.items()}
        return out

    # ------------------------------------------------------------------ projects

    async def list_projects(self, status: str | None) -> list[TenantProjectRead]:
        stmt = select(Project)
        if not self.manager:
            stmt = stmt.join(ProjectMember, ProjectMember.project_id == Project.id).where(
                ProjectMember.user_id == self.principal.user_id
            )
        stmt = stmt.where(Project.status == status) if status else stmt.where(Project.status != "archived")
        projects = list(await self.s.scalars(stmt.order_by(Project.updated_at.desc()).limit(200)))
        return [await self._read(p) for p in projects]

    async def get(self, project_id: uuid.UUID) -> TenantProjectRead:
        return await self._read(await self._project(project_id))

    async def create(self, body: ProjectCreate) -> TenantProjectRead:
        if not self.manager:
            raise Forbidden("Creating projects needs the project:manage permission")
        project = Project(tenant_id=self.tenant_id, **body.model_dump())
        self.s.add(project)
        try:
            await self.s.flush()
        except IntegrityError as exc:
            raise Conflict(f"A project with code {body.code!r} already exists") from exc
        if self.principal.user_id:
            self.s.add(
                ProjectMember(
                    tenant_id=self.tenant_id,
                    project_id=project.id,
                    user_id=self.principal.user_id,
                    role="owner",
                )
            )
        record_audit(
            self.s,
            "project.created",
            principal=self.principal,
            entity_type="project",
            entity_id=project.id,
            changes={"name": project.name},
        )
        await self.s.commit()
        await self.s.refresh(project)
        return await self._read(project)

    async def update(self, project_id: uuid.UUID, body: ProjectUpdate) -> TenantProjectRead:
        project = await self._project(project_id, edit=True)
        if project.version != body.version:
            raise VersionConflict("The project was changed by someone else; reload and try again")
        changes = body.model_dump(exclude_unset=True, exclude={"version"})
        for key, value in changes.items():
            setattr(project, key, value)
        project.version += 1
        record_audit(
            self.s,
            "project.updated",
            principal=self.principal,
            entity_type="project",
            entity_id=project.id,
            changes=changes,
        )
        try:
            await self.s.commit()
        except IntegrityError as exc:
            raise Conflict(f"A project with code {body.code!r} already exists") from exc
        await self.s.refresh(project)
        return await self._read(project)

    # ------------------------------------------------------------------ members

    async def members(self, project_id: uuid.UUID) -> list[MemberRead]:
        await self._project(project_id)
        rows = await self.s.execute(
            select(ProjectMember.user_id, User.full_name, User.email, ProjectMember.role)
            .join(User, User.id == ProjectMember.user_id)
            .where(ProjectMember.project_id == project_id)
            .order_by(User.full_name)
        )
        return [MemberRead(user_id=u, full_name=n, email=e, role=r) for u, n, e, r in rows]

    async def set_member(self, project_id: uuid.UUID, user_id: uuid.UUID, role: str) -> list[MemberRead]:
        await self._project(project_id, owner=True)
        user = await self.s.get(User, user_id)
        if user is None or user.tenant_id != self.tenant_id or user.status == UserStatus.DISABLED:
            raise NotFound("User not found in this organization")
        member = await self.s.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
            )
        )
        if member is None:
            self.s.add(
                ProjectMember(tenant_id=self.tenant_id, project_id=project_id, user_id=user_id, role=role)
            )
        else:
            if member.role == "owner" and role != "owner":
                await self._keep_an_owner(project_id, user_id)
            member.role = role
        await self.s.commit()
        return await self.members(project_id)

    async def remove_member(self, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self._project(project_id, owner=True)
        member = await self.s.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
            )
        )
        if member is None:
            raise NotFound("Not a member")
        if member.role == "owner":
            await self._keep_an_owner(project_id, user_id)
        await self.s.delete(member)
        await self.s.commit()

    async def _keep_an_owner(self, project_id: uuid.UUID, leaving: uuid.UUID) -> None:
        others = await self.s.scalar(
            select(func.count()).where(
                ProjectMember.project_id == project_id,
                ProjectMember.role == "owner",
                ProjectMember.user_id != leaving,
            )
        )
        if not others:
            raise Conflict("A project needs at least one owner; make someone else owner first")

    # ------------------------------------------------------------------ vendors

    async def vendors(self, project_id: uuid.UUID) -> list[ProjectVendorRead]:
        await self._project(project_id)
        links = list(
            await self.s.scalars(select(ProjectVendor).where(ProjectVendor.project_id == project_id))
        )
        if not links:
            return []
        ids = [link.vendor_id for link in links]
        summaries, _ = await VendorQueryService(self.s, self.principal).search(
            VendorFilters(vendor_ids=tuple(ids), include_inactive=True), limit=len(ids), cursor=None
        )
        by_id = {v.id: v for v in summaries}
        latest: dict[uuid.UUID, Scorecard] = {}
        for card in await self.s.scalars(
            select(Scorecard).where(Scorecard.project_id == project_id).order_by(Scorecard.updated_at)
        ):
            latest[card.vendor_id] = card  # later rows win
        rows = [
            ProjectVendorRead(
                vendor=by_id[link.vendor_id],
                stage=link.stage,
                note=link.note,
                added_at=link.created_at,
                scorecard=ScoreSummary(
                    scorecard_id=c.id, total_score=c.total_score, status=c.status, updated_at=c.updated_at
                )
                if (c := latest.get(link.vendor_id))
                else None,
            )
            for link in links
            if link.vendor_id in by_id
        ]
        totals: dict[uuid.UUID, Decimal] = {
            r.vendor.id: t for r in rows if r.scorecard and (t := r.scorecard.total_score) is not None
        }
        for i, vid in enumerate(sorted(totals, key=lambda v: -totals[v]), start=1):
            next(r for r in rows if r.vendor.id == vid).rank = i
        order = {"selected": 0, "shortlist": 1, "longlist": 2, "rejected": 3}
        return sorted(rows, key=lambda r: (order[r.stage], r.rank or 10_000, r.vendor.display_name.lower()))

    async def add_vendor(self, project_id: uuid.UUID, body: ProjectVendorAdd) -> list[ProjectVendorRead]:
        await self._project(project_id, edit=True)
        if await self.s.get(Vendor, body.vendor_id) is None:
            raise NotFound("Vendor not found")
        existing = await self.s.scalar(
            select(ProjectVendor).where(
                ProjectVendor.project_id == project_id, ProjectVendor.vendor_id == body.vendor_id
            )
        )
        if existing is None:
            self.s.add(ProjectVendor(tenant_id=self.tenant_id, project_id=project_id, **body.model_dump()))
        else:  # adding again just updates the stage/note
            existing.stage, existing.note = body.stage, body.note or existing.note
        await self.s.commit()
        return await self.vendors(project_id)

    async def update_vendor(
        self, project_id: uuid.UUID, vendor_id: uuid.UUID, body: ProjectVendorUpdate
    ) -> list[ProjectVendorRead]:
        await self._project(project_id, edit=True)
        link = await self.s.scalar(
            select(ProjectVendor).where(
                ProjectVendor.project_id == project_id, ProjectVendor.vendor_id == vendor_id
            )
        )
        if link is None:
            raise NotFound("This vendor isn't on the project")
        for key, value in body.model_dump(exclude_unset=True).items():
            setattr(link, key, value)
        await self.s.commit()
        return await self.vendors(project_id)

    async def remove_vendor(self, project_id: uuid.UUID, vendor_id: uuid.UUID) -> None:
        await self._project(project_id, edit=True)
        link = await self.s.scalar(
            select(ProjectVendor).where(
                ProjectVendor.project_id == project_id, ProjectVendor.vendor_id == vendor_id
            )
        )
        if link is None:
            raise NotFound("This vendor isn't on the project")
        await self.s.delete(link)
        await self.s.commit()

    async def vendor_projects(self, vendor_id: uuid.UUID) -> list[TenantProjectRead]:
        """Projects (visible to the caller) that this vendor is on, for the vendor page."""
        stmt = (
            select(Project)
            .join(ProjectVendor, ProjectVendor.project_id == Project.id)
            .where(ProjectVendor.vendor_id == vendor_id)
        )
        if not self.manager:
            stmt = stmt.join(ProjectMember, ProjectMember.project_id == Project.id).where(
                ProjectMember.user_id == self.principal.user_id
            )
        return [await self._read(p) for p in await self.s.scalars(stmt.order_by(Project.name))]

    # ------------------------------------------------------------------ templates

    async def templates(self, include_archived: bool = False) -> list[ScorecardTemplate]:
        stmt = select(ScorecardTemplate)
        if not include_archived:
            stmt = stmt.where(ScorecardTemplate.archived.is_(False))
        rows = list(await self.s.scalars(stmt.order_by(ScorecardTemplate.created_at)))
        if not rows and not include_archived:
            # Every organization starts with a sensible default it can edit.
            template = ScorecardTemplate(tenant_id=self.tenant_id, **DEFAULT_TEMPLATE)
            self.s.add(template)
            await self.s.commit()
            await self.s.refresh(template)
            rows = [template]
        return rows

    async def create_template(self, body: TemplateCreate) -> ScorecardTemplate:
        if not self.manager:
            raise Forbidden("Managing scorecard templates needs the project:manage permission")
        template = ScorecardTemplate(
            tenant_id=self.tenant_id, name=body.name, criteria=[c.model_dump() for c in body.criteria]
        )
        self.s.add(template)
        await self.s.commit()
        await self.s.refresh(template)
        return template

    async def update_template(self, template_id: uuid.UUID, body: TemplateUpdate) -> ScorecardTemplate:
        if not self.manager:
            raise Forbidden("Managing scorecard templates needs the project:manage permission")
        template = await self.s.get(ScorecardTemplate, template_id)
        if template is None:
            raise NotFound("Template not found")
        if body.name is not None:
            template.name = body.name
        if body.criteria is not None:
            template.criteria = [c.model_dump() for c in body.criteria]  # existing scorecards keep their copy
        if body.archived is not None:
            template.archived = body.archived
        await self.s.commit()
        await self.s.refresh(template)
        return template

    # ------------------------------------------------------------------ scorecards

    async def _card_read(self, card: Scorecard) -> ScorecardRead:
        out = ScorecardRead.model_validate(card)
        out.vendor_name = (
            await self.s.scalar(select(Vendor.display_name).where(Vendor.id == card.vendor_id)) or ""
        )
        if card.project_id:
            out.project_name = await self.s.scalar(select(Project.name).where(Project.id == card.project_id))
        return out

    async def _card(self, scorecard_id: uuid.UUID, *, edit: bool = False) -> Scorecard:
        card = await self.s.get(Scorecard, scorecard_id)
        if card is None:
            raise NotFound("Scorecard not found")
        if card.project_id:
            await self._project(card.project_id, edit=edit)
        if edit and not self.principal.has(P.SCORECARD_MANAGE):
            raise Forbidden("Editing scorecards needs the scorecard:manage permission")
        return card

    async def scorecards(
        self, *, project_id: uuid.UUID | None, vendor_id: uuid.UUID | None
    ) -> list[ScorecardRead]:
        stmt = select(Scorecard)
        if project_id:
            await self._project(project_id)
            stmt = stmt.where(Scorecard.project_id == project_id)
        if vendor_id:
            stmt = stmt.where(Scorecard.vendor_id == vendor_id)
        if not self.manager:  # cards on projects the caller can't see stay hidden
            visible = select(ProjectMember.project_id).where(ProjectMember.user_id == self.principal.user_id)
            stmt = stmt.where((Scorecard.project_id.is_(None)) | Scorecard.project_id.in_(visible))
        cards = list(await self.s.scalars(stmt.order_by(Scorecard.updated_at.desc()).limit(200)))
        return [await self._card_read(c) for c in cards]

    async def get_scorecard(self, scorecard_id: uuid.UUID) -> ScorecardRead:
        return await self._card_read(await self._card(scorecard_id))

    async def create_scorecard(self, body: ScorecardCreate) -> ScorecardRead:
        if not self.principal.has(P.SCORECARD_MANAGE):
            raise Forbidden("Creating scorecards needs the scorecard:manage permission")
        if body.project_id:
            await self._project(body.project_id, edit=True)
        if await self.s.get(Vendor, body.vendor_id) is None:
            raise NotFound("Vendor not found")
        if body.template_id:
            template = await self.s.get(ScorecardTemplate, body.template_id)
            if template is None:
                raise NotFound("Template not found")
        else:
            template = (await self.templates())[0]
        card = Scorecard(
            tenant_id=self.tenant_id,
            vendor_id=body.vendor_id,
            project_id=body.project_id,
            template_id=template.id,
            title=body.title or template.name,
            criteria=list(template.criteria),
            scores={},
        )
        self.s.add(card)
        await self.s.commit()
        await self.s.refresh(card)
        return await self._card_read(card)

    async def update_scorecard(self, scorecard_id: uuid.UUID, body: ScorecardUpdate) -> ScorecardRead:
        card = await self._card(scorecard_id, edit=True)
        if card.version != body.version:
            raise VersionConflict("The scorecard was changed by someone else; reload and try again")
        if card.status == "final" and body.status != "draft":
            raise Conflict("This scorecard is final; reopen it as a draft to change it")
        if body.scores is not None:
            keys = {c["key"] for c in card.criteria}
            if unknown := set(body.scores) - keys:
                raise ValidationFailed(f"Unknown criteria: {', '.join(sorted(unknown))}")
            merged = dict(card.scores)
            merged.update({k: v.model_dump() for k, v in body.scores.items()})
            card.scores = merged
            card.total_score = weighted_total(card.criteria, merged)
        if body.title is not None:
            card.title = body.title
        if body.notes is not None:
            card.notes = body.notes
        if body.status is not None:
            if body.status == "final" and card.total_score is None:
                raise ValidationFailed("Score at least one criterion before finalising")
            if body.status != card.status:
                record_audit(
                    self.s,
                    f"scorecard.{'finalised' if body.status == 'final' else 'reopened'}",
                    principal=self.principal,
                    entity_type="scorecard",
                    entity_id=card.id,
                    changes={"total_score": str(card.total_score)},
                )
            card.status = body.status
        card.version += 1
        await self.s.commit()
        await self.s.refresh(card)
        return await self._card_read(card)

    async def delete_scorecard(self, scorecard_id: uuid.UUID) -> None:
        card = await self._card(scorecard_id, edit=True)
        if card.status == "final":
            raise Conflict("Final scorecards are kept; reopen it as a draft first")
        await self.s.delete(card)
        await self.s.commit()
