from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.api.middleware import RequestContextMiddleware
from app.core.config import Settings, get_settings
from app.core.container import Container, build_container
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.modules.api_keys.router import router as api_keys_router
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.jobs.router import router as jobs_router
from app.modules.tenants.router import router as tenants_router
from app.modules.users.router import router as users_router


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    container = container or build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await app.state.container.aclose()

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=settings.app_version,
        openapi_url=f"{settings.api_prefix}/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
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
        expose_headers=["X-Request-ID", "Location", "Retry-After"],
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    v1 = APIRouter(prefix=settings.api_prefix)
    for router in (auth_router, tenants_router, users_router, api_keys_router, audit_router, jobs_router):
        v1.include_router(router)
    app.include_router(v1)
    app.include_router(health.router)
    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn app.main:app` builds the app lazily so importing this module (tests, tooling) has no side effects.
    if name == "app":
        return create_app()
    raise AttributeError(name)
