"""Postgres extensions

Revision ID: 0001
Revises:
Create Date: 2026-10-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = ["pgcrypto", "citext", "pg_trgm", "unaccent", "btree_gin", "vector"]


def upgrade() -> None:
    for ext in EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')


def downgrade() -> None:
    # Extensions may be shared with other schemas; leave them in place.
    pass
