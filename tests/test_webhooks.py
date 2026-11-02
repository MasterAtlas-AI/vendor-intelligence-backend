"""Webhooks (signed deliveries, retries, switch-off, address checks, events from approvals and jobs) and per-key API
rate limits."""

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from app.core.container import Container
from app.db.session import open_session
from app.domain.enums import ProposalItemAction, ProposalKind, ProposalOrigin
from app.domain.principal import TenantContext
from app.infra import rate_limit
from app.modules.proposals.service import NewItem, ProposalService
from app.modules.webhooks import service as webhooks
from app.modules.webhooks.models import WebhookDelivery, WebhookEndpoint
from tests.conftest import Factory
from tests.test_imports import _drain, _existing_vendor

API = "/api/v1"


def test_signature_verifies() -> None:
    body = b'{"event":"ping"}'
    header = webhooks.sign("whsec_test", body, 1_700_000_000)
    t, v1 = (part.split("=", 1)[1] for part in header.split(","))
    expected = hmac.new(b"whsec_test", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    assert t == "1700000000" and hmac.compare_digest(v1, expected)


class Receiver:
    """Stands in for the organization's server."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []
        self.status = 200
        self.address = "93.184.216.34"

    async def send(self, url: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
        self.calls.append((url, body, headers))
        return httpx.Response(self.status)

    async def resolve(self, host: str) -> list[str]:
        return [self.address]


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Iterator[Receiver]:
    r = Receiver()
    monkeypatch.setattr(webhooks, "send", r.send)
    monkeypatch.setattr(webhooks, "resolver", r.resolve)
    yield r


async def _run_deliveries(container: Container) -> None:
    """Runs queued delivery jobs that are due (other job types are drained first)."""
    await container.job_runner.run_once()


async def _deliveries(container: Container, endpoint_id: str) -> list[WebhookDelivery]:
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        return list(await s.scalars(select(WebhookDelivery).where(WebhookDelivery.endpoint_id == uuid.UUID(endpoint_id))
                                    .order_by(WebhookDelivery.created_at)))  # fmt: skip


async def test_webhooks(
    client: AsyncClient, factory: Factory, container: Container, receiver: Receiver
) -> None:
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    engineer = await factory.user(tenant, ["engineer"])

    bad = await client.post(f"{API}/webhooks", json={"url": "http://example.com/hook", "events": ["job.completed"]},
                            headers=admin.headers)  # fmt: skip
    assert bad.status_code == 422
    assert (await client.post(f"{API}/webhooks", json={"url": "https://example.com/h", "events": ["job.completed"]},
                              headers=engineer.headers)).status_code == 403  # fmt: skip
    created = await client.post(f"{API}/webhooks", headers=admin.headers, json={
        "url": "https://hooks.example.com/masteratlas", "events": ["job.completed", "proposal.approved", "vendor.updated"],
    })  # fmt: skip
    assert created.status_code == 201, created.text
    ep = created.json()
    secret = ep["secret"]
    assert secret.startswith("whsec_")
    listed = (await client.get(f"{API}/webhooks", headers=admin.headers)).json()
    assert [e["id"] for e in listed] == [ep["id"]] and "secret" not in listed[0]

    # job.completed: a report job finishing queues a delivery; the delivery job signs and sends it.
    await _drain(container.job_runner)
    report = await client.post(
        f"{API}/reports", json={"report_type": "avl", "format": "xlsx"}, headers=admin.headers
    )
    assert report.status_code == 202
    await container.job_runner.run_once()  # the report
    await _run_deliveries(container)
    [d] = await _deliveries(container, ep["id"])
    assert d.event == "job.completed" and d.status == "succeeded" and d.attempts == 1
    url, body, headers = receiver.calls[-1]
    assert url == "https://hooks.example.com/masteratlas"
    payload = json.loads(body)
    assert payload["event"] == "job.completed" and payload["data"]["type"] == "report"
    assert headers["MasterAtlas-Event"] == "job.completed" and headers["MasterAtlas-Delivery"] == str(d.id)
    t, v1 = (part.split("=", 1)[1] for part in headers["MasterAtlas-Signature"].split(","))
    assert hmac.compare_digest(
        v1, hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    )

    # proposal.approved + vendor.updated when a curator approves the organization's proposal on a listed vendor.
    tag = uuid.uuid4().hex[:6]
    vid = await _existing_vendor(container, f"Hook Pumps {tag}", f"hook-{tag}-maitest.com")
    await client.put(f"{API}/my-vendors/{vid}", json={"avl_status": "approved"}, headers=admin.headers)
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        proposal = await ProposalService(s, None).submit(
            ProposalKind.UPDATE_VENDOR, ProposalOrigin.EXCEL,
            [NewItem(ProposalItemAction.ADD_EQUIPMENT, {"category_code": "valves.ball", "name": "Ball valves"})],
            subject_vendor_id=vid, origin_tenant_id=tenant,
        )  # fmt: skip
        await s.commit()
        pid, version = proposal.id, proposal.version
    curator = await factory.user(None, platform_roles=["curator"])
    approved = await client.post(f"{API}/admin/proposals/{pid}/approve", json={"version": version},
                                 headers=curator.headers)  # fmt: skip
    assert approved.status_code == 200, approved.text
    await _drain(container.job_runner)
    events = {d.event for d in await _deliveries(container, ep["id"])}
    assert {"proposal.approved", "vendor.updated"} <= events

    # Failures retry with backoff, then fail; an endpoint failing again and again is switched off.
    receiver.status = 500
    test = await client.post(f"{API}/webhooks/{ep['id']}/test", headers=admin.headers)
    assert test.status_code == 202 and test.json()["event"] == "ping"
    await _run_deliveries(container)
    ping = next(x for x in await _deliveries(container, ep["id"]) if x.event == "ping")
    assert ping.status == "pending" and ping.attempts == 1 and ping.response_status == 500
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        await s.execute(update(WebhookDelivery).where(WebhookDelivery.id == ping.id)
                        .values(attempts=5, next_attempt_at=WebhookDelivery.created_at))  # fmt: skip
        await s.execute(
            update(WebhookEndpoint).where(WebhookEndpoint.id == uuid.UUID(ep["id"])).values(failure_count=19)
        )
        await s.commit()
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        await webhooks.queue_delivery(s)
        await s.commit()
    await _run_deliveries(container)
    ping = next(x for x in await _deliveries(container, ep["id"]) if x.event == "ping")
    assert ping.status == "failed"
    state = (await client.get(f"{API}/webhooks", headers=admin.headers)).json()[0]
    assert state["active"] is False and "Switched off" in state["disabled_reason"]
    back_on = (
        await client.patch(f"{API}/webhooks/{ep['id']}", json={"active": True}, headers=admin.headers)
    ).json()
    assert back_on["active"] and back_on["failure_count"] == 0

    # Private addresses are refused on every attempt.
    receiver.status, receiver.address = 200, "10.0.0.5"
    await client.post(f"{API}/webhooks/{ep['id']}/test", headers=admin.headers)
    await _run_deliveries(container)
    latest = (await client.get(f"{API}/webhooks/{ep['id']}/deliveries", headers=admin.headers)).json()[0]
    assert latest["event"] == "ping" and "Address not allowed" in latest["error"]

    rotated = (await client.post(f"{API}/webhooks/{ep['id']}/rotate-secret", headers=admin.headers)).json()
    assert rotated["secret"] != secret
    outsider = await factory.user(await factory.tenant(), ["tenant_admin"])
    assert (await client.get(f"{API}/webhooks", headers=outsider.headers)).json() == []
    assert (
        await client.get(f"{API}/webhooks/{ep['id']}/deliveries", headers=outsider.headers)
    ).status_code == 404


async def test_api_key_rate_limit(
    client: AsyncClient, factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One fixed moment, so every request lands in the same 60-second window however slow the test database is.
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(time=lambda: 1_800_000_030.0))
    admin = await factory.user(await factory.tenant(), ["tenant_admin"])
    created = await client.post(f"{API}/api-keys", headers=admin.headers,
                                json={"name": "limited", "scopes": ["vendor:read"], "rate_limit_per_minute": 10})  # fmt: skip
    assert created.status_code == 201, created.text
    assert created.json()["rate_limit_per_minute"] == 10
    key = {"X-API-Key": created.json()["key"]}
    statuses = [
        (await client.get(f"{API}/vendors", params={"limit": 1}, headers=key)).status_code for _ in range(11)
    ]
    assert statuses == [200] * 10 + [429]
    limited = await client.get(f"{API}/vendors", headers=key)
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    assert limited.headers["X-RateLimit-Limit"] == "10" and limited.json()["code"] == "rate_limited"
