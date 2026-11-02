"""Composition root: builds adapters once per process and exposes them to dependencies and the job runner."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.ai.service import AiService, build_provider
from app.core.config import Settings
from app.core.security import JwtManager
from app.db.session import build_engine, build_sessionmaker
from app.infra.email import EmailSender, build_email_sender
from app.infra.rate_limit import PostgresRateLimiter, RateLimiter
from app.workers.runner import JobRunner


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    jwt: JwtManager
    rate_limiter: RateLimiter
    email: EmailSender
    job_runner: JobRunner
    ai: AiService

    async def aclose(self) -> None:
        await self.job_runner.stop()
        await self.engine.dispose()


def build_container(settings: Settings) -> Container:
    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)
    ai = AiService(sessionmaker, settings, build_provider(settings))
    return Container(
        settings=settings,
        engine=engine,
        sessionmaker=sessionmaker,
        jwt=JwtManager(settings),
        rate_limiter=PostgresRateLimiter(sessionmaker),  # shared by every API instance
        email=build_email_sender(settings),
        job_runner=JobRunner(sessionmaker, settings, ai=ai),
        ai=ai,
    )
