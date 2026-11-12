"""Manufacturing capability on vendors

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-12

SOW 5.25: what a vendor can make - processes, size and weight limits, test facilities, in-house or outsourced.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS: safe if a deploy's pre-deploy step and a manual run overlap.
    op.execute(
        "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS manufacturing_capability TEXT NOT NULL DEFAULT ''"
    )


def downgrade() -> None:
    op.drop_column("vendors", "manufacturing_capability")
    _ = sa
