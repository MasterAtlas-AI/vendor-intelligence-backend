"""Startup guard: refuse to run against a database whose migrations lag behind (or run ahead of) the code."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_ROOT = Path(__file__).resolve().parents[2]


def expected_head() -> str | None:
    """The newest migration shipped with this code, or None when migrations aren't deployed alongside it."""
    ini = _ROOT / "alembic.ini"
    if not ini.exists():
        return None
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


async def schema_mismatch(engine: AsyncEngine) -> str | None:
    """A human-readable problem description, or None when the database matches the code."""
    head = expected_head()
    if head is None:
        return None
    async with engine.connect() as conn:
        current = (await conn.execute(text("SELECT max(version_num) FROM alembic_version"))).scalar()
    if current == head:
        return None
    return (
        f"Database schema is at migration {current or 'none'}, but this code needs {head}. "
        "Run `python -m alembic upgrade head` and then `python -m app.cli bootstrap`, and start the API again."
    )
