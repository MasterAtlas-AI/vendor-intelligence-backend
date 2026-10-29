"""RFQs (Phase 5)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-29

Tenant-private like projects: tenant_id NOT NULL and FORCE'd RLS on both tables.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ["rfqs", "rfq_vendors"]
CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"

DDL = [
    """
    CREATE TABLE rfqs (
        project_id UUID,
        rfq_number VARCHAR(40) NOT NULL,
        title VARCHAR(200) NOT NULL,
        description TEXT NOT NULL,
        equipment_categories VARCHAR(160)[] NOT NULL,
        requirements JSONB NOT NULL,
        due_date DATE,
        status VARCHAR(12) NOT NULL,
        awarded_vendor_id UUID,
        issued_at TIMESTAMP WITH TIME ZONE,
        awarded_at TIMESTAMP WITH TIME ZONE,
        version INTEGER NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        created_by UUID,
        updated_by UUID,
        tenant_id UUID NOT NULL,
        CONSTRAINT pk_rfqs PRIMARY KEY (id),
        CONSTRAINT uq_rfqs_tenant_number UNIQUE (tenant_id, rfq_number),
        CONSTRAINT ck_rfqs_status CHECK (status IN ('draft', 'issued', 'evaluating', 'awarded', 'closed', 'cancelled')),
        CONSTRAINT fk_rfqs_project_id_tenant_projects FOREIGN KEY(project_id) REFERENCES tenant_projects (id) ON DELETE SET NULL,
        CONSTRAINT fk_rfqs_awarded_vendor_id_vendors FOREIGN KEY(awarded_vendor_id) REFERENCES vendors (id) ON DELETE SET NULL,
        CONSTRAINT fk_rfqs_tenant_id_tenants FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_rfqs_project_id ON rfqs (project_id)
    """,
    """
    CREATE INDEX ix_rfqs_tenant_id ON rfqs (tenant_id)
    """,
    """
    CREATE TABLE rfq_vendors (
        rfq_id UUID NOT NULL,
        vendor_id UUID NOT NULL,
        status VARCHAR(12) NOT NULL,
        quote_amount NUMERIC(16, 2),
        currency CHAR(3),
        lead_time_weeks INTEGER,
        quote_received_at TIMESTAMP WITH TIME ZONE,
        notes TEXT NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        created_by UUID,
        updated_by UUID,
        tenant_id UUID NOT NULL,
        CONSTRAINT pk_rfq_vendors PRIMARY KEY (id),
        CONSTRAINT uq_rfq_vendors_rfq_vendor UNIQUE (rfq_id, vendor_id),
        CONSTRAINT ck_rfq_vendors_status CHECK (status IN ('shortlisted', 'invited', 'quoted', 'declined', 'awarded', 'lost')),
        CONSTRAINT fk_rfq_vendors_rfq_id_rfqs FOREIGN KEY(rfq_id) REFERENCES rfqs (id) ON DELETE CASCADE,
        CONSTRAINT fk_rfq_vendors_vendor_id_vendors FOREIGN KEY(vendor_id) REFERENCES vendors (id) ON DELETE CASCADE,
        CONSTRAINT fk_rfq_vendors_tenant_id_tenants FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_rfq_vendors_rfq_id ON rfq_vendors (rfq_id)
    """,
    """
    CREATE INDEX ix_rfq_vendors_tenant_id ON rfq_vendors (tenant_id)
    """,
    """
    CREATE INDEX ix_rfq_vendors_vendor_id ON rfq_vendors (vendor_id)
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
