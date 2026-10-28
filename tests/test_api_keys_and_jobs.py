import uuid

from httpx import AsyncClient

from app.core.container import Container
from app.workers.runtime import execute_job, mark_crashed
from app.workers.tasks import diagnostics_handler
from tests.conftest import Factory, FakeDispatcher


async def test_api_key_lifecycle(client: AsyncClient, factory: Factory) -> None:
    admin = await factory.user(await factory.tenant(), ["tenant_admin"])

    created = await client.post(
        "/api/v1/api-keys",
        json={"name": "ERP sync", "scopes": ["user:read", "vendor:read"]},
        headers=admin.headers,
    )
    assert created.status_code == 201, created.text
    secret = created.json()["key"]
    assert secret.startswith("mai_live_")

    listed = await client.get("/api/v1/api-keys", headers=admin.headers)
    assert "key" not in listed.json()[0] and "key_hash" not in listed.json()[0]

    as_key = {"X-API-Key": secret}
    assert (await client.get("/api/v1/users", headers=as_key)).status_code == 200
    # Scope not granted to the key:
    assert (await client.get("/api/v1/audit-logs", headers=as_key)).status_code == 403
    # Keys can't manage keys:
    assert (await client.get("/api/v1/api-keys", headers=as_key)).status_code == 403

    tampered = secret[:-1] + ("A" if secret[-1] != "A" else "B")
    assert (await client.get("/api/v1/users", headers={"X-API-Key": tampered})).status_code == 401

    assert (
        await client.delete(f"/api/v1/api-keys/{created.json()['id']}", headers=admin.headers)
    ).status_code == 204
    assert (await client.get("/api/v1/users", headers=as_key)).status_code == 401


async def test_api_key_scopes_cannot_exceed_creator_or_allowed_set(
    client: AsyncClient, factory: Factory
) -> None:
    admin = await factory.user(await factory.tenant(), ["tenant_admin"])
    resp = await client.post(
        "/api/v1/api-keys",
        json={"name": "bad", "scopes": ["apikey:manage", "knowledge:approve"]},
        headers=admin.headers,
    )
    assert resp.status_code == 422
    assert len(resp.json()["errors"]) >= 2


async def test_api_key_ip_allowlist(client: AsyncClient, factory: Factory) -> None:
    admin = await factory.user(await factory.tenant(), ["tenant_admin"])
    created = await client.post(
        "/api/v1/api-keys",
        json={"name": "office only", "scopes": ["vendor:read"], "ip_allowlist": ["203.0.113.0/24"]},
        headers=admin.headers,
    )
    resp = await client.get("/api/v1/jobs", headers={"X-API-Key": created.json()["key"]})
    assert resp.status_code == 403
    assert resp.json()["code"] == "ip_not_allowed"


async def test_diagnostics_job_dispatch_idempotency_and_execution(
    client: AsyncClient, factory: Factory, dispatcher: FakeDispatcher, container: Container
) -> None:
    super_admin = await factory.user(None, platform_roles=["super_admin"])
    headers = {**super_admin.headers, "Idempotency-Key": "diag-1"}
    body = {"steps": 2, "step_seconds": 0}

    first = await client.post("/api/v1/admin/jobs/diagnostics", json=body, headers=headers)
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]
    again = await client.post("/api/v1/admin/jobs/diagnostics", json=body, headers=headers)
    assert again.json()["job_id"] == job_id
    assert [(t, q) for t, j, q in dispatcher.calls if str(j) == job_id] == [("jobs.diagnostics", "default")]

    # Run the worker handler in-process against the same database.
    await execute_job(uuid.UUID(job_id), diagnostics_handler, container.sessionmaker)

    job = (await client.get(f"/api/v1/jobs/{job_id}", headers=super_admin.headers)).json()
    assert job["status"] == "succeeded"
    assert job["progress"] == 100
    assert job["result"]["steps"] == 2

    events = await client.get(f"/api/v1/jobs/{job_id}/events", headers=super_admin.headers)
    assert events.status_code == 200
    assert "event: completed" in events.text


async def test_job_failure_paths_are_recorded(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    super_admin = await factory.user(None, platform_roles=["super_admin"])

    async def start(body: dict[str, object]) -> str:
        resp = await client.post("/api/v1/admin/jobs/diagnostics", json=body, headers=super_admin.headers)
        return str(resp.json()["job_id"])

    # Handler raises -> failed with the error captured.
    failing = await start({"steps": 2, "step_seconds": 0, "fail_at_step": 2})
    await execute_job(uuid.UUID(failing), diagnostics_handler, container.sessionmaker)
    job = (await client.get(f"/api/v1/jobs/{failing}", headers=super_admin.headers)).json()
    assert job["status"] == "failed" and job["progress"] == 50
    assert "step 2" in job["error"]["message"]

    # Crash outside the handler -> still marked failed, never stuck in "queued".
    crashed = await start({"steps": 1, "step_seconds": 0})
    await mark_crashed(uuid.UUID(crashed), RuntimeError("boom"), container.sessionmaker)
    job = (await client.get(f"/api/v1/jobs/{crashed}", headers=super_admin.headers)).json()
    assert job["status"] == "failed" and job["error"]["code"] == "worker_crash"


async def test_jobs_are_private_to_tenant_and_owner(client: AsyncClient, factory: Factory) -> None:
    super_admin = await factory.user(None, platform_roles=["super_admin"])
    created = await client.post(
        "/api/v1/admin/jobs/diagnostics", json={"steps": 1, "step_seconds": 0}, headers=super_admin.headers
    )
    job_id = created.json()["job_id"]

    tenant_admin = await factory.user(await factory.tenant(), ["tenant_admin"])
    assert (await client.get(f"/api/v1/jobs/{job_id}", headers=tenant_admin.headers)).status_code == 404
    listed = await client.get("/api/v1/jobs", headers=tenant_admin.headers)
    assert listed.json()["items"] == []
