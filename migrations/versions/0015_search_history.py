"""Search history (Phase 4)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-29

Tenant-private with FORCE'd RLS, matched like uploads (0008): platform staff outside an organization have tenant_id
NULL and see only their own tenant-less rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"


def upgrade() -> None:
    op.create_table(
        "search_history",
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("query", sa.String(length=500), nullable=False),
        sa.Column("query_key", sa.CHAR(length=64), nullable=False),
        sa.Column("parser", sa.String(length=10), nullable=False),
        sa.Column("intent", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("took_ms", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_search_history_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_search_history_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_history")),
    )
    op.create_index(
        "ix_search_history_tenant_user_created", "search_history", ["tenant_id", "user_id", "created_at"]
    )
    op.execute("ALTER TABLE search_history ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE search_history FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON search_history
          USING (tenant_id IS NOT DISTINCT FROM {CURRENT_TENANT} OR {IS_PLATFORM})
          WITH CHECK (tenant_id IS NOT DISTINCT FROM {CURRENT_TENANT} OR {IS_PLATFORM})
        """
    )


def downgrade() -> None:
    op.drop_table("search_history")
