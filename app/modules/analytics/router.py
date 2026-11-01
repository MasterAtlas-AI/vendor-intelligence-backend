import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import ContainerDep, SessionDep, require
from app.core.exceptions import Forbidden
from app.db.session import open_session
from app.domain.permissions import P
from app.domain.principal import Principal, TenantContext
from app.modules.analytics.service import organization, platform

router = APIRouter(tags=["analytics"])

Days = Annotated[int, Query(ge=1, le=365)]


class Bar(BaseModel):
    label: str
    value: int


class KnowledgeBaseStats(BaseModel):
    vendors: int
    with_equipment: int
    avg_quality: float
    added: int  # in the period
    by_category: list[Bar]
    by_country: list[Bar]
    top_certifications: list[Bar]


class DiscoveryFunnel(BaseModel):
    runs: int
    cost_usd: float
    steps: list[Bar]  # candidates -> researched -> proposed/matched -> approved
    discarded: int
    rejected: int
    pending: int


class ApprovalStats(BaseModel):
    origin: str
    decided: int
    median_hours: float | None  # created -> decided, excluding automatic approvals
    p90_hours: float | None
    pending: int
    oldest_pending_hours: float | None


class OrganizationUsage(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    users: int
    vendors: int
    searches: int
    discovery_runs: int
    projects: int
    rfqs: int
    ai_spend_month: float


class PlatformAnalytics(BaseModel):
    days: int
    knowledge_base: KnowledgeBaseStats
    discovery_funnel: DiscoveryFunnel
    approvals: list[ApprovalStats]
    organizations: list[OrganizationUsage]


class OrganizationAnalytics(BaseModel):
    days: int
    vendors: int
    by_avl_status: list[Bar]
    by_category: list[Bar]
    by_country: list[Bar]
    rfqs_by_status: list[Bar]
    searches: int
    discovery_runs: int
    ai_spend_month: float
    ai_budget_month: float
    active_projects: int
    reports: int


@router.get("/admin/analytics", response_model=PlatformAnalytics)
async def platform_analytics(
    _: Annotated[Principal, Depends(require(P.KNOWLEDGE_APPROVE))], c: ContainerDep, days: Days = 30
) -> Any:
    """Knowledge-base coverage, the discovery funnel, approval turnaround and usage per organization."""
    async with open_session(c.sessionmaker, TenantContext.platform()) as s:
        return await platform(s, days)


@router.get("/analytics", response_model=OrganizationAnalytics)
async def organization_analytics(
    principal: Annotated[Principal, Depends(require(P.REPORT_READ))],
    session: SessionDep,
    c: ContainerDep,
    days: Days = 30,
) -> Any:
    """Your organization's vendor list, activity and AI spend."""
    if principal.tenant_id is None:
        raise Forbidden("Organization analytics need an organization; platform staff use /admin/analytics")
    budget = await c.ai.budget(principal.tenant_id)
    return await organization(session, principal.tenant_id, days, float(budget.limit_usd))
