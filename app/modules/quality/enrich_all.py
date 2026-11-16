"""AI on the whole knowledge base, as one background job: every active vendor missing a website, headquarters
country, description or contact details is looked up, field by field, in batches of 25 with the same checks as
the single look-ups (quality/lookup.py). Answers that hold up wait in Approvals; nothing changes on its own.

Progress and partial results are published after every batch, so the run can be watched live, stopped at any
time (what was done is kept), and started again later: vendors already checked for a field are skipped. The run
stops by itself at the spending limit it was given or when the AI budget is used up.
"""

import asyncio
import time
import uuid
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import and_, exists, func, or_, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models import AiUsageEvent
from app.ai.service import AINotAvailable, BudgetExceeded, CallContext
from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.enums import VendorStatus
from app.modules.jobs.models import Job
from app.modules.quality.lookup import (
    DESCRIPTION_LENGTH,
    STATUSES,
    Field_,
    LookupRow,
    estimate_usd,
    lookup_vendors,
)
from app.modules.quality.service import _CHECKS
from app.modules.vendors.models import Vendor, VendorContact, VendorDomain
from app.workers.runner import JobCancelled, JobContext, JobLeaseLost

log = get_logger(__name__)

EnrichField = Literal["website", "hq_country", "description", "contacts"]
# Websites first: contact details are read from the vendor's own site.
ORDER: tuple[EnrichField, ...] = ("website", "hq_country", "description", "contacts")
LABELS = {
    "website": "Websites",
    "hq_country": "Headquarters",
    "description": "Descriptions",
    "contacts": "Contact details",
}
MAX_ROWS_KEPT = 20_000  # rows kept in the job result (one per vendor and field)
PUBLISH_SECONDS = 3  # live progress at most this often


def _needs(field: str, r: Any) -> bool:
    if field == "website":
        return not r.site
    if field == "hq_country":
        return not r.country
    if field == "description":
        return not r.described
    return bool(r.site and not r.contacts)  # contact details are read from the vendor's own site


async def candidates_all(
    s: AsyncSession, fields: list[EnrichField], skip_checked: bool, only: list[uuid.UUID] | None = None
) -> dict[str, list[uuid.UUID]]:
    """Per field, the active vendors missing it, weakest records first (they gain the most); by default without
    those the AI already checked for it. Two queries in all, whatever the number of fields."""
    has_site = or_(
        and_(Vendor.website.is_not(None), Vendor.website != ""),
        exists().where(VendorDomain.vendor_id == Vendor.id),
    )
    has_contacts = exists().where(VendorContact.vendor_id == Vendor.id)
    rows = (
        await s.execute(
            select(
                Vendor.id,
                has_site.label("site"),
                Vendor.hq_country.is_not(None).label("country"),
                (func.coalesce(func.length(Vendor.description), 0) >= DESCRIPTION_LENGTH[0]).label(
                    "described"
                ),
                has_contacts.label("contacts"),
            )
            .where(Vendor.status == VendorStatus.ACTIVE, Vendor.id.in_(only) if only else true())
            .order_by(Vendor.data_quality_score.asc().nulls_first(), Vendor.display_name)
        )
    ).all()
    checked: set[tuple[uuid.UUID, str]] = set()
    if skip_checked:
        # _CHECKS is a fixed query of this codebase; the fields are a bound parameter.
        checked = {
            (vid, f)
            for vid, f in await s.execute(
                text(f"SELECT DISTINCT vendor_id, field FROM ({_CHECKS}) c WHERE c.field = ANY(:fields)"),  # noqa: S608
                {"fields": list(fields)},
            )
        }
    return {f: [r.id for r in rows if _needs(f, r) and (r.id, f) not in checked] for f in fields}


async def plan(
    s: AsyncSession, fields: list[EnrichField], skip_checked: bool, only: list[uuid.UUID] | None = None
) -> dict[str, int]:
    """How many vendors each field would look up."""
    return {f: len(ids) for f, ids in (await candidates_all(s, fields, skip_checked, only)).items()}


def estimate(planned: dict[str, int], settings: Settings) -> Decimal:
    return estimate_usd(sum(planned.values()), settings)


async def _spent(s: AsyncSession, job_id: uuid.UUID) -> Decimal:
    return Decimal(await s.scalar(select(func.coalesce(func.sum(AiUsageEvent.cost_usd), 0)).where(
        AiUsageEvent.job_id == job_id)) or 0)  # fmt: skip


def _row(r: Any, field: str) -> dict[str, Any]:
    return {
        "vendor_id": str(r.vendor_id),
        "name": r.name,
        "field": field,
        "status": r.status,
        "value": (r.value or "")[:200] or None,
        "proposal_id": str(r.proposal_id) if r.proposal_id else None,
        "reason": (r.reason or "")[:200] or None,
    }


async def vendor_enrich_all_handler(ctx: JobContext) -> dict[str, Any]:
    ai = ctx.ai
    if ai is None or not ai.configured:
        raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
    fields: list[EnrichField] = [f for f in ORDER if f in (ctx.input.get("fields") or ORDER)]
    skip_checked = bool(ctx.input.get("skip_checked", True))
    max_usd = Decimal(str(ctx.input.get("max_usd", 0))) or None
    only = [uuid.UUID(v) for v in ctx.input.get("vendor_ids") or []] or None
    call = CallContext(ctx.tenant_id, ctx.user_id, ctx.job_id)

    # A retry (the worker restarted) continues from the rows the earlier attempt published.
    async with ctx.session() as s:
        previous = (await s.get(Job, ctx.job_id)).result or {}  # type: ignore[union-attr]
        todo = await candidates_all(s, fields, skip_checked, only)
        wanted = {v for ids in todo.values() for v in ids}
        names = (
            dict((await s.execute(select(Vendor.id, Vendor.display_name).where(Vendor.id.in_(wanted)))).all())
            if wanted
            else {}
        )
    rows: list[dict[str, Any]] = list(previous.get("rows") or [])
    done_pairs = {(r["vendor_id"], r["field"]) for r in rows}
    for f in fields:
        todo[f] = [v for v in todo[f] if (str(v), f) not in done_pairs]
    total = len(done_pairs) + sum(len(v) for v in todo.values())
    planned = {f: len(todo[f]) + sum(1 for r in rows if r["field"] == f) for f in fields}
    stopped: str | None = None
    spent = Decimal(0)

    def snapshot(current: str | None) -> dict[str, Any]:
        counts = {f: {st: sum(1 for r in rows if r["field"] == f and r["status"] == st) for st in STATUSES}
                  for f in fields}  # fmt: skip
        return {
            "fields": fields,
            "planned": planned,
            "counts": counts,
            "checked": len(rows),
            "total": total,
            "current_field": current,
            "spent_usd": float(spent),
            "max_usd": float(max_usd) if max_usd else None,
            "skip_checked": skip_checked,
            "parallel": parallel,
            "stopped": stopped,
            "rows": rows[-MAX_ROWS_KEPT:],
        }

    def pct() -> int:
        return int(100 * len(rows) / total) if total else 100

    # One pass over everything: every (vendor, detail) pair goes into one queue and `parallel` look-ups run at a
    # time, each starting as soon as a slot frees up (no batches). The weakest vendors come first, with all their
    # details together. Not all at once: the AI provider limits requests per minute, and the spending limit must be
    # able to stop the run.
    rank = {vid: i for f in fields for i, vid in enumerate(todo[f])}
    pairs = sorted(
        ((f, vid) for f in fields for vid in todo[f]), key=lambda p: (rank[p[1]], ORDER.index(p[0]))
    )
    parallel = max(1, int(ctx.input.get("parallel") or ctx.settings.ai_enrich_parallel))
    per_lookup = estimate_usd(1, ctx.settings)
    sem = asyncio.Semaphore(parallel)
    in_flight = 0
    since_spent = 0  # look-ups finished since `spent` was last read from the usage log
    cancelled = False
    last_publish = 0.0

    async def publish(force: bool = False) -> None:
        nonlocal spent, since_spent, last_publish, cancelled
        if cancelled or (not force and time.monotonic() - last_publish < PUBLISH_SECONDS):
            return
        last_publish = time.monotonic()
        async with ctx.session() as s:
            spent, since_spent = await _spent(s, ctx.job_id), 0
        try:
            await ctx.progress(f"{len(rows)} of {total} look-ups, {in_flight} running", pct(), snapshot(None))
        except JobCancelled:
            cancelled = True  # the look-ups already running finish; no new one starts

    async def one(f: EnrichField, vid: uuid.UUID) -> None:
        nonlocal stopped, in_flight, since_spent
        async with sem:
            if stopped or cancelled:
                return
            if max_usd is not None and spent + per_lookup * (since_spent + in_flight + 1) > max_usd:
                stopped = "limit"
                return
            in_flight += 1
            try:
                field: Field_ = f
                found = await lookup_vendors(field, [vid], ctx, call)
            except BudgetExceeded:
                stopped = "budget"
                return
            except (JobCancelled, JobLeaseLost, asyncio.CancelledError):
                raise
            except Exception as exc:  # one vendor's look-up going wrong must not end the whole run
                log.warning("enrich_all.lookup_failed", vendor_id=str(vid), field=f, error=str(exc)[:300])
                found = [LookupRow(vendor_id=vid, name=names.get(vid, ""), status="failed",
                                   reason=f"Look-up failed: {type(exc).__name__}: {str(exc)[:150]}")]  # fmt: skip
            finally:
                in_flight -= 1
            since_spent += 1
            rows.extend(_row(r, f) for r in found)
        await publish()

    await ctx.progress("planning", pct(), snapshot(None))
    try:
        await ai.ensure_budget(call, per_lookup)
    except BudgetExceeded:
        stopped = "budget"
    if not stopped:
        await asyncio.gather(*(one(f, vid) for f, vid in pairs))
    if cancelled:
        raise JobCancelled
    async with ctx.session() as s:
        spent = await _spent(s, ctx.job_id)
    return snapshot(None)
