"""Integration test harness.

Integration tests need a dedicated, disposable PostgreSQL database given as TEST_DATABASE_URL (an owner
connection, e.g. a second free Render Postgres or a local install). The harness migrates it up -> down -> up
and talks to the API as a separate RLS-restricted role (`app_rw_test`), exactly like production.

Without TEST_DATABASE_URL the integration tests are skipped; pure unit tests still run.
The harness refuses to touch the databases configured in .env (DATABASE_URL / MIGRATION_DATABASE_URL),
because the down-migration drops every table.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.container import Container, build_container
from app.core.security import hash_password
from app.db.roles import ensure_app_role, generate_db_password, runtime_database_url
from app.db.session import open_session
from app.domain.enums import TenantStatus, UserStatus
from app.domain.principal import TenantContext
from app.infra.email import OutgoingEmail
from app.infra.rate_limit import InMemoryRateLimiter
from app.main import create_app
from app.modules.taxonomy.seed import sync_taxonomy
from app.modules.tenants.models import Tenant
from app.modules.users.models import User
from app.modules.users.rbac import sync_rbac
from app.modules.users.service import resolve_roles

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "Str0ng-Passw0rd!"
TEST_APP_ROLE = "app_rw_test"  # distinct from the real runtime role, in case the test DB shares a server


class TestEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    test_database_url: str | None = None
    database_url: str | None = None
    migration_database_url: str | None = None


class RecordingEmailSender:
    def __init__(self) -> None:
        self.sent: list[OutgoingEmail] = []

    async def send(self, message: OutgoingEmail) -> None:
        self.sent.append(message)

    def last_to(self, to: str) -> OutgoingEmail:
        return next(m for m in reversed(self.sent) if m.to == to)


def _target(url: str) -> tuple[str | None, int | None, str | None]:
    u = make_url(url)
    return (u.host or "").lower(), u.port or 5432, u.database


def _checked_test_url() -> str:
    env = TestEnv()
    if not env.test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set; integration tests skipped")
    target = _target(env.test_database_url)
    protected = {_target(u) for u in (env.database_url, env.migration_database_url) if u}
    if target in protected:
        pytest.exit("Refusing to run: TEST_DATABASE_URL points at the application database from .env", 2)
    if "test" not in (target[2] or "").lower():
        pytest.exit("Refusing to run: the TEST_DATABASE_URL database name must contain 'test'", 2)
    return env.test_database_url


@pytest.fixture(scope="session")
def settings() -> Settings:
    owner_url = _checked_test_url()
    return Settings(  # type: ignore[call-arg]
        _env_file=None,  # tests must not depend on a developer's local .env
        environment="test",
        database_url=owner_url,  # replaced by the runtime-role URL in `migrated`
        migration_database_url=owner_url,
        app_db_role=TEST_APP_ROLE,
        jwt_secret="test-secret-that-is-long-enough-for-hs256-signing",  # type: ignore[arg-type]
        job_runner_enabled=False,  # tests drive the runner explicitly (runner.run_once())
        job_lease_seconds=30,
        refresh_cookie_secure=False,
        log_json=False,
        log_level="WARNING",
    )


@pytest.fixture(scope="session")
async def migrated(settings: Settings) -> None:
    password = generate_db_password()
    await ensure_app_role(settings, TEST_APP_ROLE, password)
    settings.database_url = runtime_database_url(settings, TEST_APP_ROLE, password)

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.migration_database_url.replace("%", "%%"))
    cfg.attributes["app_db_role"] = TEST_APP_ROLE
    # Exercise the full down/up path once so broken downgrades are caught too.
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.fixture(scope="session")
async def container(settings: Settings, migrated: None) -> AsyncIterator[Container]:
    c = build_container(settings)
    c.email = RecordingEmailSender()
    async with open_session(c.sessionmaker, TenantContext.platform()) as s:
        await sync_rbac(s)
        await sync_taxonomy(s)
        await s.commit()
    yield c
    await c.aclose()


@pytest.fixture(scope="session")
def app(settings: Settings, container: Container):  # type: ignore[no-untyped-def]
    return create_app(settings, container)


@pytest.fixture(autouse=True)
def _fresh_rate_limits(request: pytest.FixtureRequest) -> None:
    # Only for integration tests; unit tests must not pull in the database fixtures.
    if "container" in request.fixturenames:
        request.getfixturevalue("container").rate_limiter = InMemoryRateLimiter()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture
def emails(container: Container) -> RecordingEmailSender:
    assert isinstance(container.email, RecordingEmailSender)
    return container.email


# ---------------------------------------------------------------------------- data factories


@dataclass
class Actor:
    user_id: uuid.UUID
    tenant_id: uuid.UUID | None
    email: str
    token: str = ""

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class Factory:
    def __init__(self, container: Container, client: AsyncClient) -> None:
        self.c = container
        self.client = client

    async def tenant(self, status: TenantStatus = TenantStatus.ACTIVE) -> uuid.UUID:
        async with open_session(self.c.sessionmaker, TenantContext.platform()) as s:
            slug = f"t-{uuid.uuid4().hex[:10]}"
            t = Tenant(name=f"Tenant {slug}", slug=slug, status=status, settings={}, quotas={})
            s.add(t)
            await s.commit()
            return t.id

    async def user(
        self,
        tenant_id: uuid.UUID | None,
        roles: list[str] | None = None,
        platform_roles: list[str] | None = None,
        login: bool = True,
    ) -> Actor:
        from app.modules.users.models import UserRole

        email = f"u-{uuid.uuid4().hex[:10]}@example.com"
        async with open_session(self.c.sessionmaker, TenantContext.platform()) as s:
            u = User(
                tenant_id=tenant_id,
                email=email,
                full_name="Test User",
                password_hash=hash_password(PASSWORD),
                status=UserStatus.ACTIVE,
                platform_roles=platform_roles or [],
            )
            s.add(u)
            await s.flush()
            if tenant_id and roles:
                for r in await resolve_roles(s, tenant_id, roles):
                    s.add(UserRole(user_id=u.id, role_id=r.id, tenant_id=tenant_id))
            await s.commit()
            actor = Actor(user_id=u.id, tenant_id=tenant_id, email=email)
        if login:
            resp = await self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
            assert resp.status_code == 200, resp.text
            actor.token = resp.json()["access_token"]
        return actor


@pytest.fixture
def factory(container: Container, client: AsyncClient) -> Factory:
    return Factory(container, client)
