"""Periodic housekeeping, run by the job runner's maintenance loop (and partly by `app.cli bootstrap`)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.session import open_session
from app.domain.principal import TenantContext
from app.modules.auth.models import RefreshToken
from app.modules.imports.linking import link_approved_imports
from app.modules.users.models import UserToken

log = get_logger(__name__)

AUDIT_MONTHS_AHEAD = 3
EXPIRED_TOKEN_RETENTION = timedelta(days=7)
_LOCK_KEY = "masteratlas.maintenance"


async def ensure_audit_partitions(session: AsyncSession, months_ahead: int = AUDIT_MONTHS_AHEAD) -> None:
    await session.execute(
        text(
            "SELECT ensure_audit_partition((now() + (n || ' month')::interval)::date) "
            "FROM generate_series(0, :n) AS n"
        ),
        {"n": months_ahead},
    )


async def run_maintenance(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """Returns False when another instance is already doing it (advisory lock), so replicas don't collide."""
    async with open_session(sessionmaker, TenantContext.platform()) as s:
        got_lock = await s.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": _LOCK_KEY})
        if not got_lock:
            return False
        await ensure_audit_partitions(s)
        cutoff = datetime.now(UTC) - EXPIRED_TOKEN_RETENTION
        purged_refresh = await s.execute(delete(RefreshToken).where(RefreshToken.expires_at < cutoff))
        purged_user = await s.execute(delete(UserToken).where(UserToken.expires_at < cutoff))
        await s.commit()
    log.info(
        "maintenance.done",
        purged_refresh_tokens=purged_refresh.rowcount,  # type: ignore[attr-defined]
        purged_user_tokens=purged_user.rowcount,  # type: ignore[attr-defined]
    )
    # Safety net: imported vendors whose proposal was approved but not linked right away (e.g. a crash).
    await link_approved_imports(sessionmaker)
    return True
