"""Helpers shared by Alembic revisions."""

from alembic import context

from app.core.config import get_settings


def app_db_role() -> str:
    """The runtime (RLS-restricted) role that migrations grant privileges to.

    Programmatic callers (tests) pass it via `Config.attributes["app_db_role"]`; otherwise APP_DB_ROLE from
    settings/.env is used. Read at run time, never at import time, so `.env` is honoured.
    """
    return str(context.config.attributes.get("app_db_role") or get_settings().app_db_role)
