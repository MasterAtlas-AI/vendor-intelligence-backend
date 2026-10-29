"""Duplicate detection: merge proposals and vendors.merged_into_id

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-29

Postgres can't drop enum values, so the downgrade leaves the new labels in place (harmless).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE proposal_kind ADD VALUE IF NOT EXISTS 'merge_vendors'")
    op.execute("ALTER TYPE proposal_origin ADD VALUE IF NOT EXISTS 'dedup_scan'")
    op.execute("ALTER TYPE proposal_item_action ADD VALUE IF NOT EXISTS 'merge_vendor'")
    op.add_column("vendors", sa.Column("merged_into_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_vendors_merged_into_id_vendors"), "vendors", "vendors", ["merged_into_id"], ["id"],
        ondelete="SET NULL",
    )  # fmt: skip


def downgrade() -> None:
    op.drop_constraint(op.f("fk_vendors_merged_into_id_vendors"), "vendors", type_="foreignkey")
    op.drop_column("vendors", "merged_into_id")
