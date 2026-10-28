import time
from typing import Protocol


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
