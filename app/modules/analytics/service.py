"""Analytics: numbers computed on demand from the existing tables (no extra storage).

Platform view (curators, super admins; platform session, all organizations): knowledge-base coverage, the AI
discovery funnel, approval turnaround and usage per organization. Organization view (inside the tenant's RLS
session): its vendor list, searches, AI spend, projects, RFQs and reports.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.service import month_start

AI_ORIGINS = ("ai_vendor_discovery", "ai_equipment_discovery")


def _rows(result: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in result.mappings().all()]


def _bars(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"label": r["label"] or "Unknown", "value": int(r["value"] or 0)} for r in rows]


COVERAGE_BY_CATEGORY = text("""
    SELECT top.name AS label, count(DISTINCT e.vendor_id) AS value
    FROM vendor_equipment e
    JOIN equipment_categories c ON c.id = e.category_id
    JOIN equipment_categories top ON top.code = split_part(c.code, '.', 1)
    JOIN vendors v ON v.id = e.vendor_id AND v.status = 'active'
    WHERE e.status = 'approved'
    GROUP BY top.name ORDER BY value DESC
""")
COVERAGE_BY_COUNTRY = text("""
    SELECT hq_country AS label, count(*) AS value FROM vendors
    WHERE status = 'active' GROUP BY hq_country ORDER BY value DESC LIMIT 15
""")
TOP_CERTIFICATIONS = text("""
    SELECT t.name AS label, count(DISTINCT c.vendor_id) AS value
    FROM vendor_certifications c JOIN certification_types t ON t.id = c.certification_type_id
    WHERE c.status = 'approved' GROUP BY t.name ORDER BY value DESC LIMIT 10
""")
KB_TOTALS = text("""
    SELECT
      count(*) FILTER (WHERE status = 'active') AS vendors,
      count(*) FILTER (WHERE status = 'active' AND EXISTS (
        SELECT 1 FROM vendor_equipment e WHERE e.vendor_id = vendors.id AND e.status = 'approved')) AS with_equipment,
      round(avg(data_quality_score) FILTER (WHERE status = 'active'), 3) AS avg_quality,
      count(*) FILTER (WHERE status = 'active' AND created_at >= :since) AS added
    FROM vendors
""")
FUNNEL_JOBS = text("""
    SELECT count(*) AS runs,
      coalesce(sum((result->>'candidates')::int), 0) AS candidates,
      coalesce(sum((result->>'researched')::int), 0) AS researched,
      coalesce(sum((result->'counts'->>'proposed')::int), 0) AS proposed,
      coalesce(sum((result->'counts'->>'matched')::int), 0) AS matched,
      coalesce(sum((result->'counts'->>'discarded')::int), 0) AS discarded,
      coalesce(sum(cost_usd), 0) AS cost
    FROM jobs WHERE type = 'vendor_discovery' AND status = 'succeeded' AND created_at >= :since
""")
FUNNEL_PROPOSALS = text("""
    SELECT
      count(*) FILTER (WHERE status IN ('approved', 'partially_approved', 'auto_approved')) AS approved,
      count(*) FILTER (WHERE status = 'rejected') AS rejected,
      count(*) FILTER (WHERE status IN ('pending_review', 'needs_info')) AS pending
    FROM knowledge_proposals
    WHERE origin IN ('ai_vendor_discovery', 'ai_equipment_discovery') AND created_at >= :since
""")
APPROVAL_SLA = text("""
    SELECT origin::text AS origin,
      count(*) FILTER (WHERE decided_at IS NOT NULL AND decided_at >= :since) AS decided,
      percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM decided_at - created_at) / 3600)
        FILTER (WHERE decided_at IS NOT NULL AND decided_at >= :since AND status <> 'auto_approved') AS median_hours,
      percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM decided_at - created_at) / 3600)
        FILTER (WHERE decided_at IS NOT NULL AND decided_at >= :since AND status <> 'auto_approved') AS p90_hours,
      count(*) FILTER (WHERE status IN ('pending_review', 'needs_info')) AS pending,
      max(extract(epoch FROM now() - created_at) / 3600)
        FILTER (WHERE status IN ('pending_review', 'needs_info')) AS oldest_pending_hours
    FROM knowledge_proposals GROUP BY origin ORDER BY pending DESC, decided DESC
""")
TENANT_USAGE = text("""
    SELECT t.id, t.name, t.status::text AS status,
      (SELECT count(*) FROM users u WHERE u.tenant_id = t.id AND u.status = 'active') AS users,
      (SELECT count(*) FROM tenant_vendor_links l WHERE l.tenant_id = t.id) AS vendors,
      (SELECT count(*) FROM search_history h WHERE h.tenant_id = t.id AND h.created_at >= :since) AS searches,
      (SELECT count(*) FROM jobs j WHERE j.tenant_id = t.id AND j.type IN
         ('vendor_discovery', 'equipment_discovery', 'service_discovery', 'enrichment')
         AND j.created_at >= :since) AS discovery_runs,
      (SELECT count(*) FROM tenant_projects p WHERE p.tenant_id = t.id AND p.status <> 'archived') AS projects,
      (SELECT count(*) FROM rfqs r WHERE r.tenant_id = t.id) AS rfqs,
      (SELECT coalesce(sum(a.cost_usd), 0) FROM ai_usage_events a
         WHERE a.tenant_id = t.id AND a.created_at >= :month) AS ai_spend_month
    FROM tenants t ORDER BY ai_spend_month DESC, vendors DESC, t.name LIMIT 200
""")


async def platform(s: AsyncSession, days: int) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    params = {"since": since}
    totals = (await s.execute(KB_TOTALS, params)).mappings().one()
    jobs = (await s.execute(FUNNEL_JOBS, params)).mappings().one()
    props = (await s.execute(FUNNEL_PROPOSALS, params)).mappings().one()
    sla = _rows(await s.execute(APPROVAL_SLA, params))
    usage = _rows(await s.execute(TENANT_USAGE, {"since": since, "month": month_start()}))
    return {
        "days": days,
        "knowledge_base": {
            "vendors": int(totals["vendors"] or 0),
            "with_equipment": int(totals["with_equipment"] or 0),
            "avg_quality": float(totals["avg_quality"] or 0),
            "added": int(totals["added"] or 0),
            "by_category": _bars(_rows(await s.execute(COVERAGE_BY_CATEGORY))),
            "by_country": _bars(_rows(await s.execute(COVERAGE_BY_COUNTRY))),
            "top_certifications": _bars(_rows(await s.execute(TOP_CERTIFICATIONS))),
        },
        "discovery_funnel": {
            "runs": int(jobs["runs"] or 0),
            "cost_usd": float(jobs["cost"] or 0),
            "steps": [
                {"label": "Candidates found", "value": int(jobs["candidates"] or 0)},
                {"label": "Researched", "value": int(jobs["researched"] or 0)},
                {
                    "label": "Proposed or matched",
                    "value": int(jobs["proposed"] or 0) + int(jobs["matched"] or 0),
                },
                {"label": "Approved by curators", "value": int(props["approved"] or 0)},
            ],
            "discarded": int(jobs["discarded"] or 0),
            "rejected": int(props["rejected"] or 0),
            "pending": int(props["pending"] or 0),
        },
        "approvals": [
            {
                "origin": r["origin"],
                "decided": int(r["decided"] or 0),
                "median_hours": None if r["median_hours"] is None else round(float(r["median_hours"]), 1),
                "p90_hours": None if r["p90_hours"] is None else round(float(r["p90_hours"]), 1),
                "pending": int(r["pending"] or 0),
                "oldest_pending_hours": None
                if r["oldest_pending_hours"] is None
                else round(float(r["oldest_pending_hours"]), 1),
            }
            for r in sla
        ],
        "organizations": [
            {
                **{k: r[k] for k in ("name", "status")},
                "id": r["id"],
                **{
                    k: int(r[k] or 0)
                    for k in ("users", "vendors", "searches", "discovery_runs", "projects", "rfqs")
                },
                "ai_spend_month": float(r["ai_spend_month"] or 0),
            }
            for r in usage
        ],
    }


ORG_AVL = text("""
    SELECT avl_status::text AS label, count(*) AS value FROM tenant_vendor_links
    WHERE tenant_id = :tenant GROUP BY avl_status ORDER BY value DESC
""")
ORG_BY_CATEGORY = text("""
    SELECT top.name AS label, count(DISTINCT e.vendor_id) AS value
    FROM tenant_vendor_links l
    JOIN vendor_equipment e ON e.vendor_id = l.vendor_id AND e.status = 'approved'
    JOIN equipment_categories c ON c.id = e.category_id
    JOIN equipment_categories top ON top.code = split_part(c.code, '.', 1)
    WHERE l.tenant_id = :tenant
    GROUP BY top.name ORDER BY value DESC
""")
ORG_BY_COUNTRY = text("""
    SELECT v.hq_country AS label, count(*) AS value
    FROM tenant_vendor_links l JOIN vendors v ON v.id = l.vendor_id
    WHERE l.tenant_id = :tenant GROUP BY v.hq_country ORDER BY value DESC LIMIT 10
""")
ORG_ACTIVITY = text("""
    SELECT
      (SELECT count(*) FROM search_history WHERE tenant_id = :tenant AND created_at >= :since) AS searches,
      (SELECT count(*) FROM jobs WHERE tenant_id = :tenant AND created_at >= :since AND type IN
         ('vendor_discovery', 'equipment_discovery', 'service_discovery', 'enrichment')) AS discovery_runs,
      (SELECT coalesce(sum(cost_usd), 0) FROM ai_usage_events WHERE tenant_id = :tenant AND created_at >= :month)
        AS ai_spend_month,
      (SELECT count(*) FROM tenant_projects WHERE tenant_id = :tenant AND status = 'active') AS active_projects,
      (SELECT count(*) FROM reports WHERE tenant_id = :tenant AND created_at >= :since) AS reports
""")
ORG_RFQS = text("""
    SELECT status AS label, count(*) AS value FROM rfqs WHERE tenant_id = :tenant GROUP BY status ORDER BY value DESC
""")


# ---------------------------------------------------------------- the organization's own project history (SOW 23)

HIST_BY_CATEGORY = text("""
    SELECT top.name AS label, count(*) AS value
    FROM project_equipment e
    JOIN equipment_categories c ON c.id = e.category_id
    JOIN equipment_categories top ON top.code = split_part(c.code, '.', 1)
    WHERE e.tenant_id = :tenant AND e.verification <> 'archived'
    GROUP BY top.name ORDER BY value DESC
""")
HIST_MOST_USED = text("""
    SELECT coalesce(v.display_name, max(e.vendor_name)) AS label, count(DISTINCT e.project_id) AS value
    FROM project_equipment e LEFT JOIN vendors v ON v.id = e.vendor_id
    WHERE e.tenant_id = :tenant AND e.verification <> 'archived' AND (e.vendor_id IS NOT NULL OR e.vendor_name <> '')
    GROUP BY coalesce(e.vendor_id::text, lower(e.vendor_name)), v.display_name
    ORDER BY value DESC, label LIMIT 15
""")
HIST_BY_YEAR = text("""
    SELECT p.year::text AS label, count(*) AS value
    FROM project_equipment e JOIN tenant_projects p ON p.id = e.project_id
    WHERE e.tenant_id = :tenant AND e.verification <> 'archived' AND p.year IS NOT NULL
    GROUP BY p.year ORDER BY p.year
""")
HIST_TOTALS = text("""
    SELECT
      (SELECT count(*) FROM project_equipment WHERE tenant_id = :tenant AND verification <> 'archived') AS equipment,
      (SELECT count(*) FROM tenant_projects WHERE tenant_id = :tenant AND kind = 'historical') AS historical_projects,
      (SELECT count(*) FROM procurement_records WHERE tenant_id = :tenant) AS purchase_orders,
      (SELECT count(*) FROM knowledge_notes WHERE tenant_id = :tenant) AS notes,
      (SELECT count(*) FROM project_equipment WHERE tenant_id = :tenant AND created_at >= :since) AS added_recently,
      (SELECT count(*) FROM mel_rows WHERE tenant_id = :tenant AND status = 'pending') AS awaiting_review
""")
HIST_LONG_LEAD = text("""
    SELECT coalesce(c.name, e.name, 'Unclassified') AS category, coalesce(v.display_name, r.vendor_name) AS vendor,
           r.lead_time_weeks AS weeks, p.name AS project
    FROM procurement_records r
    JOIN tenant_projects p ON p.id = r.project_id
    LEFT JOIN project_equipment e ON e.id = r.equipment_id
    LEFT JOIN equipment_categories c ON c.id = e.category_id
    LEFT JOIN vendors v ON v.id = r.vendor_id
    WHERE r.tenant_id = :tenant AND r.lead_time_weeks IS NOT NULL
    ORDER BY r.lead_time_weeks DESC LIMIT 10
""")
HIST_PERFORMANCE = text("""
    SELECT coalesce(v.display_name, max(r.vendor_name)) AS vendor, count(*) AS orders,
           avg(r.lead_time_weeks) AS avg_lead_weeks, avg(r.on_time::int) AS on_time_rate,
           avg(r.performance_rating) AS avg_rating
    FROM procurement_records r LEFT JOIN vendors v ON v.id = r.vendor_id
    WHERE r.tenant_id = :tenant
    GROUP BY coalesce(r.vendor_id::text, lower(r.vendor_name)), v.display_name
    HAVING count(*) > 0
    ORDER BY orders DESC LIMIT 15
""")


def _num(value: Any, digits: int = 2) -> float | None:
    return round(float(value), digits) if value is not None else None


async def history(s: AsyncSession, tenant_id: uuid.UUID, days: int) -> dict[str, Any]:
    params = {"tenant": tenant_id, "since": datetime.now(UTC) - timedelta(days=days)}
    totals = (await s.execute(HIST_TOTALS, params)).mappings().one()
    return {
        **{k: int(v or 0) for k, v in totals.items()},
        "equipment_by_category": _bars(_rows(await s.execute(HIST_BY_CATEGORY, params))),
        "most_used_vendors": _bars(_rows(await s.execute(HIST_MOST_USED, params))),
        "equipment_by_year": _bars(_rows(await s.execute(HIST_BY_YEAR, params))),
        "long_lead": [
            {
                "category": r["category"],
                "vendor": r["vendor"] or "",
                "weeks": int(r["weeks"]),
                "project": r["project"],
            }
            for r in _rows(await s.execute(HIST_LONG_LEAD, params))
        ],
        "vendor_performance": [
            {
                "vendor": r["vendor"] or "Unknown",
                "orders": int(r["orders"]),
                "avg_lead_weeks": _num(r["avg_lead_weeks"], 1),
                "on_time_rate": _num(r["on_time_rate"]),
                "avg_rating": _num(r["avg_rating"], 1),
            }
            for r in _rows(await s.execute(HIST_PERFORMANCE, params))
        ],
    }


async def organization(s: AsyncSession, tenant_id: uuid.UUID, days: int, budget_usd: float) -> dict[str, Any]:
    params = {"tenant": tenant_id, "since": datetime.now(UTC) - timedelta(days=days), "month": month_start()}
    activity = (await s.execute(ORG_ACTIVITY, params)).mappings().one()
    avl = _bars(_rows(await s.execute(ORG_AVL, params)))
    return {
        "days": days,
        "vendors": sum(b["value"] for b in avl),
        "by_avl_status": avl,
        "by_category": _bars(_rows(await s.execute(ORG_BY_CATEGORY, params))),
        "by_country": _bars(_rows(await s.execute(ORG_BY_COUNTRY, params))),
        "rfqs_by_status": _bars(_rows(await s.execute(ORG_RFQS, params))),
        "searches": int(activity["searches"] or 0),
        "discovery_runs": int(activity["discovery_runs"] or 0),
        "ai_spend_month": float(activity["ai_spend_month"] or 0),
        "ai_budget_month": budget_usd,
        "active_projects": int(activity["active_projects"] or 0),
        "reports": int(activity["reports"] or 0),
        "history": await history(s, tenant_id, days),
    }
