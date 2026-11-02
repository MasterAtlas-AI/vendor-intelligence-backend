"""Webhooks: events are written as deliveries in the same transaction as the change, then sent by a background job.

Signing (verify on the receiving side): header `MasterAtlas-Signature: t=<unix seconds>,v1=<hex>`, where v1 is
HMAC-SHA256 of `"<t>.<raw body>"` with the endpoint's secret. Reject old timestamps (e.g. > 5 minutes) to stop
replays. Each delivery also carries `MasterAtlas-Event` and `MasterAtlas-Delivery` (an id, for de-duplication).

Retries: 1 min, 5 min, 30 min, 2 h, 6 h, then the delivery is marked failed. An endpoint that fails 20 times in a row
is switched off (an organization admin can switch it back on). Target addresses are checked like the crawler's: no
private, loopback or link-local hosts, on every attempt.
"""

import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.crawler.urls import BlockedURL, check_url, system_resolver
from app.db.session import open_session
from app.domain.enums import JobStatus, JobType
from app.domain.principal import TenantContext
from app.modules.jobs.models import Job
from app.modules.tenant_vendors.models import TenantVendorLink
from app.modules.webhooks.models import WebhookDelivery, WebhookEndpoint
from app.workers.runner import JobContext

log = get_logger(__name__)

BACKOFF = [
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
]
DISABLE_AFTER = 20
BATCH = 100
TIMEOUT_SECONDS = 10.0
USER_AGENT = "MasterAtlas-Webhooks/1"

# Replaced in tests: (url) -> httpx response, and the DNS resolver used by the address check.
Sender = Callable[[str, bytes, dict[str, str]], Awaitable[httpx.Response]]
resolver = system_resolver


async def _http_send(url: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        return await client.post(url, content=body, headers=headers)


send: Sender = _http_send


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def sign(secret: str, body: bytes, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


async def emit(s: AsyncSession, tenant_id: uuid.UUID, event: str, data: dict[str, Any]) -> int:
    """Queues `event` for the organization's active endpoints subscribed to it (in the caller's transaction).
    The session must be allowed to write the tenant's rows (its own tenant, or platform mode)."""
    endpoints = list(
        await s.scalars(
            select(WebhookEndpoint).where(
                WebhookEndpoint.tenant_id == tenant_id,
                WebhookEndpoint.active.is_(True),
                WebhookEndpoint.events.any(event),  # type: ignore[arg-type]
            )
        )
    )
    now = datetime.now(UTC)
    for ep in endpoints:
        s.add(
            WebhookDelivery(
                tenant_id=tenant_id,
                endpoint_id=ep.id,
                event=event,
                next_attempt_at=now,
                payload={"event": event, "created_at": now.isoformat(), "data": data},
            )
        )
    if endpoints:
        await queue_delivery(s)
    return len(endpoints)


async def queue_delivery(s: AsyncSession, run_after: datetime | None = None) -> None:
    """One delivery job at a time: queue it unless one is already waiting for the same moment or earlier."""
    when = run_after or datetime.now(UTC)
    waiting = await s.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.type == JobType.WEBHOOK_DELIVERY, Job.status == JobStatus.QUEUED, Job.run_after <= when)
    )
    if not waiting:
        s.add(Job(tenant_id=None, user_id=None, type=JobType.WEBHOOK_DELIVERY, input={}, run_after=when))


async def emit_vendor_updated(s: AsyncSession, vendor_id: uuid.UUID, name: str, version: int) -> int:
    """vendor.updated goes to every organization that has the vendor on its list (platform session)."""
    tenants = list(
        await s.scalars(select(TenantVendorLink.tenant_id).where(TenantVendorLink.vendor_id == vendor_id))
    )
    total = 0
    for tenant_id in tenants:
        total += await emit(
            s,
            tenant_id,
            "vendor.updated",
            {"vendor_id": str(vendor_id), "name": name, "knowledge_version": version},
        )
    return total


async def deliver(d: WebhookDelivery, ep: WebhookEndpoint) -> None:
    """One attempt. Updates the delivery and the endpoint's health (caller commits)."""
    body = json.dumps({"id": str(d.id), **d.payload}, separators=(",", ":"), default=str).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "MasterAtlas-Event": d.event,
        "MasterAtlas-Delivery": str(d.id),
        "MasterAtlas-Signature": sign(ep.secret, body, int(time.time())),
    }
    now = datetime.now(UTC)
    d.attempts += 1
    ok = False
    try:
        await check_url(ep.url, resolver)
        response = await send(ep.url, body, headers)
        d.response_status = response.status_code
        ok = 200 <= response.status_code < 300
        d.error = None if ok else f"HTTP {response.status_code}"
    except BlockedURL as exc:
        d.error = f"Address not allowed: {exc}"
    except httpx.HTTPError as exc:
        d.error = f"{type(exc).__name__}: {exc}"[:500]
    ep.last_delivery_at, ep.last_status = now, d.response_status
    if ok:
        d.status, d.delivered_at = "succeeded", now
        ep.failure_count = 0
        return
    ep.failure_count += 1
    if ep.failure_count >= DISABLE_AFTER:
        ep.active = False
        ep.disabled_reason = f"Switched off after {DISABLE_AFTER} failed deliveries in a row"
    if d.attempts > len(BACKOFF) or not ep.active:
        d.status = "failed"
    else:
        d.next_attempt_at = now + BACKOFF[d.attempts - 1]


async def webhook_delivery_handler(ctx: JobContext) -> dict[str, Any]:
    sent = failed = 0
    async with open_session(ctx.sessionmaker, TenantContext.platform()) as s:
        due = list(
            await s.scalars(
                select(WebhookDelivery)
                .where(
                    WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= datetime.now(UTC)
                )
                .order_by(WebhookDelivery.next_attempt_at)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        )
        endpoints = {
            ep.id: ep
            for ep in await s.scalars(
                select(WebhookEndpoint).where(WebhookEndpoint.id.in_({d.endpoint_id for d in due}))
            )
        }
        for d in due:
            await deliver(d, endpoints[d.endpoint_id])
            sent += d.status == "succeeded"
            failed += d.status != "succeeded"
        later = await s.scalar(
            select(func.min(WebhookDelivery.next_attempt_at)).where(WebhookDelivery.status == "pending")
        )
        if later is not None:
            await queue_delivery(s, max(later, datetime.now(UTC)))
        await s.commit()
    return {"delivered": sent, "not_delivered": failed}
