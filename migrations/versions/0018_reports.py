"""Generated reports (Phase 5)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-29

Tenant-private (FORCE'd RLS); the file itself is stored in `data` like uploads.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"

DDL = [
    """
    CREATE TABLE reports (
        user_id UUID,
        job_id UUID,
        report_type VARCHAR(30) NOT NULL,
        format VARCHAR(5) NOT NULL,
        title VARCHAR(200) NOT NULL,
        parameters JSONB NOT NULL,
        status VARCHAR(10) NOT NULL,
        error TEXT,
        filename VARCHAR(200),
        content_type VARCHAR(100),
        size_bytes INTEGER,
        data BYTEA,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        created_by UUID,
        updated_by UUID,
        tenant_id UUID NOT NULL,
        CONSTRAINT pk_reports PRIMARY KEY (id),
        CONSTRAINT ck_reports_report_type CHECK (report_type IN ('vendor_comparison', 'market_scan', 'avl', 'rfq_summary')),
        CONSTRAINT ck_reports_format CHECK (format IN ('pdf', 'xlsx')),
        CONSTRAINT ck_reports_status CHECK (status IN ('queued', 'ready', 'failed')),
        CONSTRAINT fk_reports_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT fk_reports_job_id_jobs FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE SET NULL,
        CONSTRAINT fk_reports_tenant_id_tenants FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_reports_tenant_created ON reports (tenant_id, created_at)
    """,
    """
    CREATE INDEX ix_reports_tenant_id ON reports (tenant_id)
    """,
]


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)
    op.execute("ALTER TABLE reports ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reports FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON reports
          USING (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
          WITH CHECK (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE reports")
