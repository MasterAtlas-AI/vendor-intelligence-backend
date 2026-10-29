"""Data quality of the knowledge base: a 0-1 score per vendor, the issues behind it, and a curator dashboard.

Score (stored in vendors.data_quality_score, also used by search ranking):
  0.15 has a website · 0.10 headquarters country · 0.10 description · 0.25 approved equipment/service facts
  (full marks at 5) · 0.10 certifications · 0.20 facts backed by a source (share of facts with evidence)
  · 0.10 freshness (website re-read in the last year)

Scoring is one SQL statement over all vendors (cheap enough for the maintenance loop). Re-checking stale vendors
re-reads their websites with AI (a few cents each), so it only runs when a curator asks, with a cap.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

STALE_DAYS = 365

# Per-vendor facts used by both the score and the issue list.
_FACTS = """
WITH f AS (
  SELECT v.id,
    (v.website IS NOT NULL OR EXISTS (SELECT 1 FROM vendor_domains d WHERE d.vendor_id = v.id)) AS has_site,
    v.hq_country IS NOT NULL AS has_country,
    length(coalesce(v.description, '')) >= 40 AS has_description,
    (SELECT count(*) FROM vendor_equipment e WHERE e.vendor_id = v.id AND e.status = 'approved')
      + (SELECT count(*) FROM vendor_services s WHERE s.vendor_id = v.id AND s.status = 'approved') AS facts,
    (SELECT count(*) FROM vendor_certifications c WHERE c.vendor_id = v.id AND c.status = 'approved') AS certs,
    (SELECT count(*) FROM vendor_certifications c WHERE c.vendor_id = v.id AND c.status = 'approved'
       AND c.valid_to IS NOT NULL AND c.valid_to < current_date) AS expired_certs,
    (SELECT count(DISTINCT fs.fact_id) FROM fact_sources fs
       WHERE fs.fact_table IN ('vendor_equipment', 'vendor_services', 'vendor_certifications')
         AND fs.fact_id IN (SELECT e.id FROM vendor_equipment e WHERE e.vendor_id = v.id AND e.status = 'approved'
                            UNION ALL SELECT s.id FROM vendor_services s WHERE s.vendor_id = v.id AND s.status = 'approved'
                            UNION ALL SELECT c.id FROM vendor_certifications c
                              WHERE c.vendor_id = v.id AND c.status = 'approved')) AS sourced,
    coalesce(v.last_verified_at, v.created_at) < now() - make_interval(days => :stale_days) AS stale
  FROM vendors v
  WHERE v.status <> 'retired' {where}
)
"""

_SCORE = """
  round((
      0.15 * has_site::int + 0.10 * has_country::int + 0.10 * has_description::int
    + 0.25 * least(facts, 5) / 5.0 + 0.10 * (certs > 0)::int
    + 0.20 * CASE WHEN facts + certs = 0 THEN 0 ELSE sourced::numeric / (facts + certs) END
    + 0.10 * (NOT stale)::int
  )::numeric, 3)
"""

ISSUES = {
    "no_website": "No website",
    "no_country": "Headquarters country unknown",
    "no_description": "No description",
    "no_facts": "No equipment or services",
    "unsourced_facts": "Facts without a source",
    "expired_certifications": "Expired certifications",
    "stale": f"Not re-checked for over {STALE_DAYS // 30} months",
}

_ISSUE_SQL = {
    "no_website": "NOT has_site",
    "no_country": "NOT has_country",
    "no_description": "NOT has_description",
    "no_facts": "facts = 0",
    "unsourced_facts": "sourced < facts + certs",
    "expired_certifications": "expired_certs > 0",
    "stale": "stale",
}


@dataclass
class QualityRow:
    vendor_id: uuid.UUID
    name: str
    country: str | None
    score: float
    issues: list[str]


async def rescore(s: AsyncSession, vendor_ids: list[uuid.UUID] | None = None) -> int:
    """Recomputes data_quality_score (all vendors, or the given ones). Returns how many changed."""
    where = "AND v.id = ANY(:ids)" if vendor_ids else ""
    params: dict[str, Any] = {"stale_days": STALE_DAYS}
    if vendor_ids:
        params["ids"] = vendor_ids
    result = await s.execute(
        text(
            _FACTS.format(where=where)
            + f"UPDATE vendors v SET data_quality_score = {_SCORE.replace(chr(10), ' ')} FROM f "
            "WHERE v.id = f.id AND v.data_quality_score IS DISTINCT FROM " + _SCORE.replace(chr(10), " ")
        ),
        params,
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def dashboard(s: AsyncSession, *, issue: str | None, limit: int) -> dict[str, Any]:
    params: dict[str, Any] = {"stale_days": STALE_DAYS}
    base = _FACTS.format(where="")
    counts_sql = ", ".join(f"count(*) FILTER (WHERE {cond}) AS {key}" for key, cond in _ISSUE_SQL.items())
    totals = (
        (
            await s.execute(
                text(
                    base + f"SELECT count(*) AS vendors, round(avg({_SCORE})::numeric, 3) AS avg_score, "
                    "count(*) FILTER (WHERE " + _SCORE + " < 0.4) AS low, "
                    "count(*) FILTER (WHERE " + _SCORE + " >= 0.4 AND " + _SCORE + " < 0.7) AS medium, "
                    "count(*) FILTER (WHERE " + _SCORE + " >= 0.7) AS high, " + counts_sql + " FROM f"
                ),
                params,
            )
        )
        .mappings()
        .one()
    )
    issue_cols = ", ".join(f"({cond}) AS {key}" for key, cond in _ISSUE_SQL.items())
    where = f"WHERE {_ISSUE_SQL[issue]}" if issue in _ISSUE_SQL else ""
    rows = (
        (
            await s.execute(
                text(
                    base  # `where` comes from the fixed _ISSUE_SQL table
                    + f"SELECT f.id, v.display_name, v.hq_country, {_SCORE} AS score, {issue_cols} "
                    f"FROM f JOIN vendors v ON v.id = f.id {where} ORDER BY score, v.display_name LIMIT :limit"
                ),
                {**params, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return {
        "vendors": totals["vendors"],
        "avg_score": float(totals["avg_score"] or 0),
        "distribution": {"low": totals["low"], "medium": totals["medium"], "high": totals["high"]},
        "issues": [{"key": k, "label": ISSUES[k], "count": totals[k]} for k in ISSUES],
        "worst": [
            QualityRow(
                r["id"], r["display_name"], r["hq_country"], float(r["score"]), [k for k in ISSUES if r[k]]
            )
            for r in rows
        ],
    }


async def stale_with_website(s: AsyncSession, limit: int) -> list[uuid.UUID]:
    """The stalest active vendors that have a website to re-read (re-check candidates)."""
    cutoff = datetime.now(UTC) - timedelta(days=STALE_DAYS)
    rows = await s.execute(
        text(
            "SELECT v.id FROM vendors v WHERE v.status = 'active' "
            "AND (v.website IS NOT NULL OR EXISTS (SELECT 1 FROM vendor_domains d WHERE d.vendor_id = v.id)) "
            "AND coalesce(v.last_verified_at, v.created_at) < :cutoff "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.type = 'enrichment' AND j.status IN ('queued', 'running') "
            "  AND j.input->>'vendor_id' = v.id::text) "
            "ORDER BY coalesce(v.last_verified_at, v.created_at) LIMIT :limit"
        ),
        {"cutoff": cutoff, "limit": limit},
    )
    return [r[0] for r in rows]
