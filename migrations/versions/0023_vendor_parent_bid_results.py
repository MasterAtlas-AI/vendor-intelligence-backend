"""Parent company on vendors; quotations and technical bid evaluation results on procurement records

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-11

SOW 3 / 5.1 / 9: "Which vendors have previously been technically accepted by SPE?" needs the result of each technical
bid evaluation; a quotation is recorded like a PO, with record_type = 'quotation'.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("vendors", sa.Column("parent_company", sa.String(300), nullable=True))
    op.add_column(
        "procurement_records", sa.Column("record_type", sa.String(12), server_default="po", nullable=False)
    )
    op.add_column("procurement_records", sa.Column("technical_status", sa.String(12), nullable=True))
    op.create_check_constraint("record_type", "procurement_records", "record_type IN ('po', 'quotation')")
    op.create_check_constraint(
        "technical_status",
        "procurement_records",
        "technical_status IS NULL OR technical_status IN ('accepted', 'conditional', 'rejected')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_procurement_records_technical_status"), "procurement_records", type_="check")
    op.drop_constraint(op.f("ck_procurement_records_record_type"), "procurement_records", type_="check")
    op.drop_column("procurement_records", "technical_status")
    op.drop_column("procurement_records", "record_type")
    op.drop_column("vendors", "parent_company")
