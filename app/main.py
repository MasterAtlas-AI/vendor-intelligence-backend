from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import DBAPIError

from app.ai.service import register_ai_error_handler
from app.api import health
from app.api.middleware import RequestContextMiddleware
from app.core.config import Settings, get_settings
from app.core.container import Container, build_container
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.db.schema_check import schema_mismatch
from app.modules.ai.router import router as ai_router
from app.modules.analytics.router import router as analytics_router
from app.modules.api_keys.router import router as api_keys_router
from app.modules.assistant.router import router as assistant_router
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.discovery.router import router as discovery_router
from app.modules.imports.router import router as imports_router
from app.modules.jobs.router import router as jobs_router
from app.modules.projects.router import router as projects_router
from app.modules.proposals.router import router as proposals_router
from app.modules.quality.router import router as quality_router
from app.modules.reports.router import router as reports_router
from app.modules.rfqs.router import router as rfqs_router
from app.modules.search.router import router as search_router
from app.modules.taxonomy.router import router as taxonomy_router
from app.modules.tenants.router import router as tenants_router
from app.modules.users.router import router as users_router
from app.modules.vendors.router import router as vendors_router
from app.modules.webhooks.router import router as webhooks_router

log = get_logger(__name__)


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    container = container or build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        c: Container = app.state.container
        # Fail fast with one clear message instead of SQL errors on every request and job poll.
        try:
            problem = await schema_mismatch(c.engine)
        except (DBAPIError, OSError) as exc:
            host = c.engine.url.host
            log.error("startup.database_unreachable", host=host, error=str(exc)[:300])
            await c.engine.dispose()
            raise RuntimeError(
                f"Can't connect to the database at {host}. If it's on Render, check in the dashboard that the "
                "database is available (not suspended) and that Access Control allows this machine's public IP."
            ) from None
        if problem:
            log.error("startup.schema_mismatch", detail=problem)
            await c.engine.dispose()
            raise RuntimeError(problem)
        if c.settings.job_runner_enabled:
            await c.job_runner.start()
        yield
        await c.aclose()  # stops the runner first, then closes the pool

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=settings.app_version,
        openapi_url=f"{settings.api_prefix}/openapi.json",
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=f"{settings.api_prefix}/redoc",
        lifespan=lifespan,
    )
    app.state.container = container

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-API-Key",
            "X-Act-As-Tenant",
            "Idempotency-Key",
            "X-Request-ID",
        ],
        expose_headers=["X-Request-ID", "Location", "Retry-After", "X-RateLimit-Limit"],
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    register_ai_error_handler(app)

    v1 = APIRouter(prefix=settings.api_prefix)
    for router in (
        auth_router,
        tenants_router,
        users_router,
        api_keys_router,
        audit_router,
        jobs_router,
        taxonomy_router,
        vendors_router,
        proposals_router,
        imports_router,
        ai_router,
        discovery_router,
        search_router,
        projects_router,
        assistant_router,
        quality_router,
        rfqs_router,
        reports_router,
        analytics_router,
        webhooks_router,
    ):
        v1.include_router(router)
    app.include_router(v1)
    app.include_router(health.router)

    # The docs moved under the versioned prefix; keep the old bookmarks working.
    for old, new in (("/docs", f"{settings.api_prefix}/docs"), ("/redoc", f"{settings.api_prefix}/redoc")):
        app.add_api_route(old, lambda new=new: RedirectResponse(new), include_in_schema=False)
    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn app.main:app` builds the app lazily so importing this module (tests, tooling) has no side effects.
    if name == "app":
        return create_app()
    raise AttributeError(name)
