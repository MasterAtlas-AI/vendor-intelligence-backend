import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import text

from app.db.session import open_session
from app.domain.principal import TenantContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        """Count one hit; return False when the limit for the current window is exceeded."""
        ...


class InMemoryRateLimiter:
    """Fixed-window counter per process. Limits are per API instance, which is fine for a single instance;
    a shared (Postgres-backed) limiter is needed before scaling out horizontally."""

    _MAX_BUCKETS = 50_000

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[int, float]] = {}  # bucket -> (count, expires_at)

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        window = int(now // window_seconds)
        bucket = f"{key}:{window_seconds}:{window}"
        count, expires_at = self._buckets.get(bucket, (0, (window + 1) * window_seconds))
        self._buckets[bucket] = (count + 1, expires_at)
        if len(self._buckets) > self._MAX_BUCKETS:
            # Keep memory bounded: drop buckets whose window has ended.
            self._buckets = {k: v for k, v in self._buckets.items() if v[1] > now}
        return count + 1 <= limit


class PostgresRateLimiter:
    """Fixed-window counters in Postgres (`api_rate_counters`), shared by every API instance. One upsert per hit;
    expired windows are deleted by the maintenance loop."""

    def __init__(self, sessionmaker: "async_sessionmaker[AsyncSession]") -> None:
        self.sessionmaker = sessionmaker

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        return (await self.count(key, window_seconds)) <= limit

    async def count(self, key: str, window_seconds: int) -> int:
        """Counts one hit and returns the window's total so far."""
        now = time.time()
        window = int(now // window_seconds)
        expires = datetime.fromtimestamp((window + 1) * window_seconds, UTC)
        async with open_session(self.sessionmaker, TenantContext.platform()) as s:
            total = await s.scalar(
                text(
                    "INSERT INTO api_rate_counters (bucket, count, expires_at) VALUES (:b, 1, :e) "
                    "ON CONFLICT (bucket) DO UPDATE SET count = api_rate_counters.count + 1 RETURNING count"
                ),
                {"b": f"{key}:{window_seconds}:{window}"[:200], "e": expires},
            )
            await s.commit()
        return int(total or 0)


def seconds_left(window_seconds: int) -> int:
    return max(1, window_seconds - int(time.time()) % window_seconds)
