import time
from collections import defaultdict
from typing import Protocol

from redis.asyncio import Redis


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        """Count one hit; return False when the limit for the current window is exceeded."""
        ...


class RedisRateLimiter:
    """Fixed-window counter; one INCR per hit, shared across API replicas."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        window = int(time.time() // window_seconds)
        redis_key = f"rl:{key}:{window}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds)
            count, _ = await pipe.execute()
        return int(count) <= limit


class InMemoryRateLimiter:
    """Single-process limiter for tests and local runs without Redis."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        window = int(time.time() // window_seconds)
        bucket = f"{key}:{window}"
        self._counts[bucket] += 1
        return self._counts[bucket] <= limit
