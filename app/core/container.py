"""Composition root: builds adapters once per process and exposes them to dependencies and workers."""

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import JwtManager
from app.db.session import build_engine, build_sessionmaker
from app.infra.email import EmailSender, build_email_sender
from app.infra.queue import CeleryJobDispatcher, JobDispatcher
from app.infra.rate_limit import InMemoryRateLimiter, RateLimiter, RedisRateLimiter


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    jwt: JwtManager
    rate_limiter: RateLimiter
    email: EmailSender
    jobs: JobDispatcher

    async def aclose(self) -> None:
        await self.engine.dispose()
        await self.redis.aclose()


def build_container(settings: Settings) -> Container:
    from app.workers.celery_app import celery_app  # local import: keeps Celery out of import-time cycles

    engine = build_engine(settings)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    limiter: RateLimiter = (
        RedisRateLimiter(redis) if settings.rate_limit_backend == "redis" else InMemoryRateLimiter()
    )
    return Container(
        settings=settings,
        engine=engine,
        sessionmaker=build_sessionmaker(engine),
        redis=redis,
        jwt=JwtManager(settings),
        rate_limiter=limiter,
        email=build_email_sender(settings),
        jobs=CeleryJobDispatcher(celery_app),
    )
