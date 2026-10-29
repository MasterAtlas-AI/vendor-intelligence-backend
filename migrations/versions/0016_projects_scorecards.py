"""Projects, shortlists and scorecards (Phase 4)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-29

Tenant-private like tenant_vendor_links: tenant_id NOT NULL and FORCE'd RLS on every table.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ["tenant_projects", "project_members", "project_vendors", "scorecard_templates", "scorecards"]
CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"

DDL = [
    """
    CREATE TABLE tenant_projects (
        code VARCHAR(40),
        name VARCHAR(200) NOT NULL,
        client VARCHAR(200),
        facility_type VARCHAR(60),
        country CHAR(2),
        description TEXT NOT NULL,
        status VARCHAR(20) NOT NULL,
        version INTEGER NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        created_by UUID,
        updated_by UUID,
        tenant_id UUID NOT NULL,
        CONSTRAINT pk_tenant_projects PRIMARY KEY (id),
        CONSTRAINT uq_tenant_projects_tenant_code UNIQUE (tenant_id, code),
        CONSTRAINT ck_tenant_projects_status CHECK (status IN ('active', 'on_hold', 'closed', 'archived')),
        CONSTRAINT fk_tenant_projects_tenant_id_tenants FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_tenant_projects_tenant_id ON tenant_projects (tenant_id)
    """,
    """
    CREATE TABLE project_members (
        project_id UUID NOT NULL,
        user_id UUID NOT NULL,
        role VARCHAR(10) NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        created_by UUID,
        updated_by UUID,
        tenant_id UUID NOT NULL,
        CONSTRAINT pk_project_members PRIMARY KEY (id),
        CONSTRAINT uq_project_members_project_user UNIQUE (project_id, user_id),
        CONSTRAINT ck_project_members_role CHECK (role IN ('owner', 'editor', 'viewer')),
        CONSTRAINT fk_project_members_project_id_tenant_projects FOREIGN KEY(project_id) REFERENCES tenant_projects (id) ON DELETE CASCADE,
        CONSTRAINT fk_project_members_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
        CONSTRAINT fk_project_members_tenant_id_tenants FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_project_members_project_id ON project_members (project_id)
    """,
    """
    CREATE INDEX ix_project_members_tenant_id ON project_members (tenant_id)
    """,
    """
    CREATE INDEX ix_project_members_user_id ON project_members (user_id)
    """,
    """
    CREATE TABLE project_vendors (
        project_id UUID NOT NULL,
        vendor_id UUID NOT NULL,
        stage VARCHAR(12) NOT NULL,
        note TEXT NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        created_by UUID,
        updated_by UUID,
        tenant_id UUID NOT NULL,
        CONSTRAINT pk_project_vendors PRIMARY KEY (id),
        CONSTRAINT uq_project_vendors_project_vendor UNIQUE (project_id, vendor_id),
        CONSTRAINT ck_project_vendors_stage CHECK (stage IN ('longlist', 'shortlist', 'selected', 'rejected')),
        CONSTRAINT fk_project_vendors_project_id_tenant_projects FOREIGN KEY(project_id) REFERENCES tenant_projects (id) ON DELETE CASCADE,
        CONSTRAINT fk_project_vendors_vendor_id_vendors FOREIGN KEY(vendor_id) REFERENCES vendors (id) ON DELETE CASCADE,
        CONSTRAINT fk_project_vendors_tenant_id_tenants FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_project_vendors_project_id ON project_vendors (project_id)
    """,
    """
    CREATE INDEX ix_project_vendors_tenant_id ON project_vendors (tenant_id)
    """,
    """
    CREATE INDEX ix_project_vendors_vendor_id ON project_vendors (vendor_id)
    """,
    """
    CREATE TABLE scorecard_templates (
        name VARCHAR(120) NOT NULL,
        criteria JSONB NOT NULL,
        archived BOOLEAN NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        created_by UUID,
        updated_by UUID,
        tenant_id UUID NOT NULL,
        CONSTRAINT pk_scorecard_templates PRIMARY KEY (id),
        CONSTRAINT fk_scorecard_templates_tenant_id_tenants FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_scorecard_templates_tenant_id ON scorecard_templates (tenant_id)
    """,
    """
    CREATE TABLE scorecards (
        vendor_id UUID NOT NULL,
        project_id UUID,
        template_id UUID,
        title VARCHAR(200) NOT NULL,
        criteria JSONB NOT NULL,
        scores JSONB NOT NULL,
        total_score NUMERIC(5, 2),
        status VARCHAR(10) NOT NULL,
        notes TEXT NOT NULL,
        version INTEGER NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        created_by UUID,
        updated_by UUID,
        tenant_id UUID NOT NULL,
        CONSTRAINT pk_scorecards PRIMARY KEY (id),
        CONSTRAINT ck_scorecards_status CHECK (status IN ('draft', 'final')),
        CONSTRAINT fk_scorecards_vendor_id_vendors FOREIGN KEY(vendor_id) REFERENCES vendors (id) ON DELETE CASCADE,
        CONSTRAINT fk_scorecards_project_id_tenant_projects FOREIGN KEY(project_id) REFERENCES tenant_projects (id) ON DELETE CASCADE,
        CONSTRAINT fk_scorecards_template_id_scorecard_templates FOREIGN KEY(template_id) REFERENCES scorecard_templates (id) ON DELETE SET NULL,
        CONSTRAINT fk_scorecards_tenant_id_tenants FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_scorecards_project_id ON scorecards (project_id)
    """,
    """
    CREATE INDEX ix_scorecards_tenant_id ON scorecards (tenant_id)
    """,
    """
    CREATE INDEX ix_scorecards_tenant_vendor ON scorecards (tenant_id, vendor_id)
    """,
]


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
              WITH CHECK (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
            """
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
