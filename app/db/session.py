"""Engine/session factories and the hooks that bind every transaction to a tenant context.

RLS policies read `app.tenant_id` / `app.is_platform`. They are set with `set_config(..., is_local => true)`
at the start of *every* transaction, so they can never leak across pooled connections or commits.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.domain.principal import TenantContext

TENANT_CTX_KEY = "tenant_ctx"
ACTOR_KEY = "actor_id"


def build_engine(settings: Settings, *, url: str | None = None, null_pool: bool = False) -> AsyncEngine:
    kwargs: dict[str, Any] = {"echo": settings.db_echo, "pool_pre_ping": True}
    if null_pool:
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update(pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow)
    return create_async_engine(url or settings.database_url, **kwargs)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def open_session(
    sessionmaker: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    actor_id: uuid.UUID | None = None,
) -> AsyncIterator[AsyncSession]:
    """A session bound to a tenant context. Uncommitted work is rolled back on exit."""
    async with sessionmaker() as session:
        session.info[TENANT_CTX_KEY] = ctx
        session.info[ACTOR_KEY] = actor_id
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(session: Session, _tx: SessionTransaction, connection: Connection) -> None:
    ctx: TenantContext | None = session.info.get(TENANT_CTX_KEY)
    if ctx is None:
        raise RuntimeError("Session used without a TenantContext; use db.session.open_session()")
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tid, true), set_config('app.is_platform', :plat, true)"),
        {"tid": str(ctx.tenant_id) if ctx.tenant_id else "", "plat": "on" if ctx.is_platform else "off"},
    )


@event.listens_for(Session, "before_flush")
def _stamp_authorship(session: Session, _flush_context: Any, _instances: Any) -> None:
    actor = session.info.get(ACTOR_KEY)
    if actor is None:
        return
    for obj in session.new:
        if hasattr(obj, "created_by") and obj.created_by is None:
            obj.created_by = actor
    for obj in session.dirty:
        if hasattr(obj, "updated_by"):
            obj.updated_by = actor
