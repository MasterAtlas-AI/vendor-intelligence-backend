"""Import rows double as AI discovery results: a 'discarded' outcome and a details column

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-29

Postgres can't drop an enum value, so the downgrade leaves 'discarded' in import_row_outcome (harmless).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE import_row_outcome ADD VALUE IF NOT EXISTS 'discarded'")
    op.add_column(
        "import_rows",
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("import_rows", "details")
