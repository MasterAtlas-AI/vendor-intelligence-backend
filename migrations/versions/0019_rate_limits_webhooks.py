"""API key rate limits and webhooks (Phase 5)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-01

api_keys.rate_limit_per_minute; api_rate_counters (internal, written in platform mode, no RLS); webhook_endpoints and
webhook_deliveries (tenant-private, FORCE'd RLS).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("webhook_endpoints", "webhook_deliveries")
CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"

DDL = [
    "ALTER TABLE api_keys ADD COLUMN rate_limit_per_minute INTEGER DEFAULT 120 NOT NULL",
    """
    CREATE TABLE api_rate_counters (
        bucket VARCHAR(200) NOT NULL,
        count INTEGER NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT pk_api_rate_counters PRIMARY KEY (bucket)
    )
    """,
    """
    CREATE INDEX ix_api_rate_counters_expires_at ON api_rate_counters (expires_at)
    """,
    """
    CREATE TABLE webhook_endpoints (
        url VARCHAR(500) NOT NULL,
        secret VARCHAR(100) NOT NULL,
        events VARCHAR(40)[] NOT NULL,
        description VARCHAR(200) NOT NULL,
        active BOOLEAN NOT NULL,
        failure_count INTEGER NOT NULL,
        disabled_reason VARCHAR(200),
        last_delivery_at TIMESTAMP WITH TIME ZONE,
        last_status INTEGER,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        created_by UUID,
        updated_by UUID,
        tenant_id UUID NOT NULL,
        CONSTRAINT pk_webhook_endpoints PRIMARY KEY (id),
        CONSTRAINT fk_webhook_endpoints_tenant_id_tenants FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_webhook_endpoints_tenant_id ON webhook_endpoints (tenant_id)
    """,
    """
    CREATE TABLE webhook_deliveries (
        endpoint_id UUID NOT NULL,
        event VARCHAR(40) NOT NULL,
        payload JSONB NOT NULL,
        status VARCHAR(10) NOT NULL,
        attempts INTEGER NOT NULL,
        next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
        response_status INTEGER,
        error TEXT,
        delivered_at TIMESTAMP WITH TIME ZONE,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        created_by UUID,
        updated_by UUID,
        tenant_id UUID NOT NULL,
        CONSTRAINT pk_webhook_deliveries PRIMARY KEY (id),
        CONSTRAINT ck_webhook_deliveries_status CHECK (status IN ('pending', 'succeeded', 'failed')),
        CONSTRAINT fk_webhook_deliveries_endpoint_id_webhook_endpoints FOREIGN KEY(endpoint_id) REFERENCES webhook_endpoints (id) ON DELETE CASCADE,
        CONSTRAINT fk_webhook_deliveries_tenant_id_tenants FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_webhook_deliveries_due ON webhook_deliveries (status, next_attempt_at)
    """,
    """
    CREATE INDEX ix_webhook_deliveries_endpoint_id ON webhook_deliveries (endpoint_id)
    """,
    """
    CREATE INDEX ix_webhook_deliveries_tenant_id ON webhook_deliveries (tenant_id)
    """,
]


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)
    for table in TENANT_TABLES:
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
    op.execute("DROP TABLE webhook_deliveries")
    op.execute("DROP TABLE webhook_endpoints")
    op.execute("DROP TABLE api_rate_counters")
    op.execute("ALTER TABLE api_keys DROP COLUMN rate_limit_per_minute")
