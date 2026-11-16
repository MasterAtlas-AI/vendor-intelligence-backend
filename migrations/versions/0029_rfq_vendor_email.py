"""RFQ vendors remember when the RFQ was e-mailed to them, and to which address

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("rfq_vendors", sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("rfq_vendors", sa.Column("emailed_to", sa.String(254), nullable=True))


def downgrade() -> None:
    op.drop_column("rfq_vendors", "emailed_to")
    op.drop_column("rfq_vendors", "emailed_at")
