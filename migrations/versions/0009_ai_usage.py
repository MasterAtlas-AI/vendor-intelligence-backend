"""AI usage ledger (cost metering and monthly budgets)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-29

Tenant-private with FORCE'd RLS. tenant_id is NULL for platform staff working outside an organization, so the policy
matches with IS NOT DISTINCT FROM, like uploads (0008).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"


def upgrade() -> None:
    op.create_table(
        "ai_usage_events",
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("prompt", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("web_search_calls", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_ai_usage_events_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_ai_usage_events_job_id_jobs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_usage_events")),
    )
    op.create_index(
        "ix_ai_usage_events_tenant_id_created_at",
        "ai_usage_events",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_ai_usage_events_job_id"), "ai_usage_events", ["job_id"], unique=False)

    op.execute("ALTER TABLE ai_usage_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ai_usage_events FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON ai_usage_events
          USING (tenant_id IS NOT DISTINCT FROM {CURRENT_TENANT} OR {IS_PLATFORM})
          WITH CHECK (tenant_id IS NOT DISTINCT FROM {CURRENT_TENANT} OR {IS_PLATFORM})
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_usage_events_job_id"), table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_tenant_id_created_at", table_name="ai_usage_events")
    op.drop_table("ai_usage_events")
