"""Provisioning of the runtime database role (run with the owner/migration connection)."""

import re
import secrets

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.session import build_engine

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def generate_db_password() -> str:
    # URL-safe alphabet: can be embedded in a connection URL and a SQL literal without escaping.
    return secrets.token_urlsafe(32)


async def ensure_app_role(settings: Settings, role: str, password: str) -> None:
    """Create (or reset the password of) a LOGIN role with no superuser/BYPASSRLS rights.

    Requires an owner connection with CREATEROLE (Render's default database user has it).
    Table privileges are granted by migration 0003, so run this *before* `alembic upgrade head`.
    """
    if not _IDENT.match(role):
        raise ValueError(f"invalid role name: {role!r}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", password):
        raise ValueError("password must be at least 24 URL-safe characters")
    database = make_url(settings.migration_database_url).database
    engine = build_engine(settings, url=settings.migration_database_url, null_pool=True)
    try:
        async with engine.begin() as conn:
            exists = (
                await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
            ).scalar()
            # DDL can't take bind parameters; both values were validated against strict patterns above.
            if exists:
                # Rotation only: managed Postgres (Render) rejects ALTER ROLE ... NOSUPERUSER/NOBYPASSRLS from
                # non-superusers, and the attributes were fixed at creation anyway.
                await conn.execute(text(f"ALTER ROLE {role} LOGIN PASSWORD '{password}'"))
            else:
                await conn.execute(
                    text(
                        f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
                        "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
                    )
                )
            flags = (
                await conn.execute(
                    text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = :r"), {"r": role}
                )
            ).scalar()
            if flags:
                raise RuntimeError(f"role {role} can bypass Row-Level Security; refusing to use it")
            await conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO {role}'))
    finally:
        await engine.dispose()


def runtime_database_url(settings: Settings, role: str, password: str) -> str:
    """The owner URL rewritten to log in as the runtime role."""
    url = make_url(settings.migration_database_url).set(username=role, password=password)
    return url.render_as_string(hide_password=False)
