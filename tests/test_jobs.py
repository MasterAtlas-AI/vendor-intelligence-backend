"""Postgres job queue: claiming, leases and crash recovery, cancellation, idempotency, live progress.

Tests drive the runner explicitly (`run_once` / `claim_next`) so they are deterministic; one test also runs the
real background loop. Jobs are global to the test database, so tests first drain leftovers from other tests.
"""

import asyncio
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from app.core.container import Container
from app.db.session import open_session
from app.domain.principal import TenantContext
from app.workers.maintenance import run_maintenance
from app.workers.runner import JobRunner
from tests.conftest import Actor, Factory

DIAG = "/api/v1/admin/jobs/diagnostics"


async def _drain(runner: JobRunner) -> None:
    while await runner.run_once():
        pass


async def _start(client: AsyncClient, admin: Actor, **body: Any) -> str:
    resp = await client.post(DIAG, json={"step_seconds": 0, **body}, headers=admin.headers)
    assert resp.status_code == 202, resp.text
    return str(resp.json()["job_id"])


async def _job(client: AsyncClient, admin: Actor, job_id: str) -> dict[str, Any]:
    resp = await client.get(f"/api/v1/jobs/{job_id}", headers=admin.headers)
    assert resp.status_code == 200
    return dict(resp.json())


async def _sql(container: Container, sql: str, **params: Any) -> None:
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        await s.execute(text(sql), params)
        await s.commit()


async def test_job_runs_to_completion_with_progress_stream(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    admin = await factory.user(None, platform_roles=["super_admin"])
    await _drain(container.job_runner)
    job_id = await _start(client, admin, steps=2)
    assert (await _job(client, admin, job_id))["status"] == "queued"

    assert await container.job_runner.run_once() == uuid.UUID(job_id)

    job = await _job(client, admin, job_id)
    assert job["status"] == "succeeded" and job["progress"] == 100 and job["attempts"] == 1
    assert job["result"]["steps"] == 2
    events = await client.get(f"/api/v1/jobs/{job_id}/events", headers=admin.headers)
    assert "event: completed" in events.text
    assert "no-transform" in events.headers["cache-control"]  # keeps compressing proxies from buffering it


async def test_idempotency_key_returns_same_job(client: AsyncClient, factory: Factory) -> None:
    admin = await factory.user(None, platform_roles=["super_admin"])
    headers = {**admin.headers, "Idempotency-Key": f"diag-{uuid.uuid4().hex}"}
    body = {"steps": 1, "step_seconds": 0}
    first = await client.post(DIAG, json=body, headers=headers)
    again = await client.post(DIAG, json=body, headers=headers)
    assert first.json()["job_id"] == again.json()["job_id"]


async def test_handler_failure_is_recorded(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    admin = await factory.user(None, platform_roles=["super_admin"])
    await _drain(container.job_runner)
    job_id = await _start(client, admin, steps=2, fail_at_step=2)
    await container.job_runner.run_once()

    job = await _job(client, admin, job_id)
    assert job["status"] == "failed" and job["progress"] == 50
    assert "step 2" in job["error"]["message"]


async def test_two_runners_never_claim_the_same_job(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    admin = await factory.user(None, platform_roles=["super_admin"])
    await _drain(container.job_runner)
    created = {await _start(client, admin, steps=1), await _start(client, admin, steps=1)}
    other = JobRunner(container.sessionmaker, container.settings)

    claimed = await asyncio.gather(container.job_runner.claim_next(), other.claim_next())

    assert None not in claimed
    assert {str(c) for c in claimed} == created
    await asyncio.gather(container.job_runner.execute(claimed[0]), other.execute(claimed[1]))  # type: ignore[arg-type]
    assert [(await _job(client, admin, j))["status"] for j in created] == ["succeeded", "succeeded"]


async def test_crashed_worker_job_is_recovered_and_stale_result_discarded(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    admin = await factory.user(None, platform_roles=["super_admin"])
    await _drain(container.job_runner)
    job_id = await _start(client, admin, steps=1)

    crashed = JobRunner(container.sessionmaker, container.settings)
    assert await crashed.claim_next() == uuid.UUID(job_id)
    # The crashed worker never renews its lease; simulate the lease running out.
    await _sql(
        container, "UPDATE jobs SET locked_until = now() - interval '1 second' WHERE id = :id", id=job_id
    )

    assert await container.job_runner.run_once() == uuid.UUID(job_id)
    job = await _job(client, admin, job_id)
    assert job["status"] == "succeeded" and job["attempts"] == 2

    # If the "crashed" worker wakes up later it must not overwrite the result.
    await crashed.execute(uuid.UUID(job_id))
    assert (await _job(client, admin, job_id))["result"]["attempt"] == 2


async def test_job_fails_after_max_attempts(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    admin = await factory.user(None, platform_roles=["super_admin"])
    await _drain(container.job_runner)
    job_id = await _start(client, admin, steps=1)
    await _sql(
        container,
        "UPDATE jobs SET status = 'running', attempts = max_attempts, locked_by = 'dead-worker', "
        "locked_until = now() - interval '1 second' WHERE id = :id",
        id=job_id,
    )

    await container.job_runner.run_once()

    job = await _job(client, admin, job_id)
    assert job["status"] == "failed" and job["error"]["code"] == "lease_expired"


async def test_cancel_queued_and_running_jobs(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    admin = await factory.user(None, platform_roles=["super_admin"])
    await _drain(container.job_runner)

    queued = await _start(client, admin, steps=1)
    assert (await client.post(f"/api/v1/jobs/{queued}/cancel", headers=admin.headers)).json()[
        "status"
    ] == "cancelled"
    assert await container.job_runner.run_once() is None  # never picked up

    running = await _start(client, admin, steps=2)
    job_uuid = await container.job_runner.claim_next()
    assert str(job_uuid) == running
    assert (await client.post(f"/api/v1/jobs/{running}/cancel", headers=admin.headers)).status_code == 200
    await container.job_runner.execute(job_uuid)  # type: ignore[arg-type]
    assert (await _job(client, admin, running))["status"] == "cancelled"
    assert (await client.post(f"/api/v1/jobs/{running}/cancel", headers=admin.headers)).status_code == 409


async def test_background_loop_picks_up_new_jobs(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    admin = await factory.user(None, platform_roles=["super_admin"])
    await _drain(container.job_runner)
    await container.job_runner.start(maintenance=False)
    try:
        job_id = await _start(client, admin, steps=2)
        for _ in range(100):
            if (await _job(client, admin, job_id))["status"] == "succeeded":
                break
            await asyncio.sleep(0.1)
        assert (await _job(client, admin, job_id))["status"] == "succeeded"
    finally:
        await container.job_runner.stop()
    assert not container.job_runner.running


async def test_jobs_are_private_to_tenant_and_owner(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    super_admin = await factory.user(None, platform_roles=["super_admin"])
    job_id = await _start(client, super_admin, steps=1)

    tenant_admin = await factory.user(await factory.tenant(), ["tenant_admin"])
    assert (await client.get(f"/api/v1/jobs/{job_id}", headers=tenant_admin.headers)).status_code == 404
    assert (await client.get("/api/v1/jobs", headers=tenant_admin.headers)).json()["items"] == []
    await _drain(container.job_runner)


async def test_maintenance_runs_as_app_role(container: Container) -> None:
    assert await run_maintenance(container.sessionmaker) is True
