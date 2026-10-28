"""Tenancy, identity, RBAC, API keys, audit log, jobs

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = {
    "tenant_status": ("trial", "active", "suspended", "closed"),
    "user_status": ("invited", "active", "disabled"),
    "user_token_purpose": ("invite", "password_reset", "email_verify"),
    "actor_type": ("user", "api_key", "system", "ai"),
    "job_status": ("queued", "running", "succeeded", "partially_succeeded", "failed", "cancelled"),
}


def enum(name: str) -> pg.ENUM:
    return pg.ENUM(*ENUMS[name], name=name, create_type=False)


def entity_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", pg.UUID(as_uuid=True), nullable=True),
    ]


def tenant_fk(nullable: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "tenant_id",
        pg.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE", name=None),
        nullable=nullable,
    )


def upgrade() -> None:
    for name, values in ENUMS.items():
        pg.ENUM(*values, name=name).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tenants",
        *entity_columns(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", pg.CITEXT(), nullable=False),
        sa.Column("status", enum("tenant_status"), nullable=False, server_default="trial"),
        sa.Column("plan", sa.String(50), nullable=False, server_default="starter"),
        sa.Column("trusted_source", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("settings", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("quotas", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )

    op.create_table(
        "permissions",
        *entity_columns(),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("key", name="uq_permissions_key"),
    )

    op.create_table(
        "roles",
        *entity_columns(),
        tenant_fk(nullable=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "uq_roles_tenant_id_key",
        "roles",
        ["tenant_id", "key"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )

    op.create_table(
        "role_permissions",
        sa.Column(
            "role_id", pg.UUID(as_uuid=True), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "permission_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("permissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    op.create_table(
        "users",
        *entity_columns(),
        tenant_fk(nullable=True),
        sa.Column("email", pg.CITEXT(), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.Text()),
        sa.Column("status", enum("user_status"), nullable=False, server_default="invited"),
        sa.Column("platform_roles", pg.ARRAY(sa.String(32)), nullable=False, server_default="{}"),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("perms_version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "user_roles",
        sa.Column(
            "user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "role_id", pg.UUID(as_uuid=True), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
        ),
        tenant_fk(),
    )
    op.create_index("ix_user_roles_tenant_id", "user_roles", ["tenant_id"])

    op.create_table(
        "user_tokens",
        *entity_columns(),
        sa.Column(
            "user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        tenant_fk(nullable=True),
        sa.Column("purpose", enum("user_token_purpose"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("token_hash", name="uq_user_tokens_token_hash"),
    )
    op.create_index("ix_user_tokens_user_id", "user_tokens", ["user_id"])

    op.create_table(
        "refresh_tokens",
        *entity_columns(),
        sa.Column(
            "user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        tenant_fk(nullable=True),
        sa.Column("family_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "replaced_by_id", pg.UUID(as_uuid=True), sa.ForeignKey("refresh_tokens.id", ondelete="SET NULL")
        ),
        sa.Column("ip", pg.INET()),
        sa.Column("user_agent", sa.Text()),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.create_table(
        "api_keys",
        *entity_columns(),
        tenant_fk(),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("scopes", pg.ARRAY(sa.String(64)), nullable=False, server_default="{}"),
        sa.Column("ip_allowlist", pg.ARRAY(pg.CIDR()), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("prefix", name="uq_api_keys_prefix"),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])

    op.create_table(
        "jobs",
        *entity_columns(),
        tenant_fk(nullable=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("status", enum("job_status"), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(64)),
        sa.Column("progress", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("input", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", pg.JSONB()),
        sa.Column("error", pg.JSONB()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("celery_task_id", sa.String(64)),
        sa.Column("queue", sa.String(32), nullable=False, server_default="default"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="progress_range"),
    )
    op.create_index("ix_jobs_tenant_id_created_at", "jobs", ["tenant_id", "created_at"])
    op.create_index(
        "uq_jobs_tenant_id_idempotency_key",
        "jobs",
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        postgresql_nulls_not_distinct=True,
    )

    # Audit log: append-only, monthly range partitions. A DEFAULT partition catches anything outside
    # the pre-created months; `ensure_audit_partition()` is called by the maintenance task.
    op.execute(
        """
        CREATE TABLE audit_logs (
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            created_at timestamptz NOT NULL DEFAULT now(),
            tenant_id uuid NULL,
            actor_type actor_type NOT NULL,
            actor_id uuid NULL,
            action varchar(100) NOT NULL,
            entity_type varchar(64) NULL,
            entity_id uuid NULL,
            changes jsonb NOT NULL DEFAULT '{}'::jsonb,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            ip inet NULL,
            user_agent text NULL,
            request_id varchar(64) NULL,
            CONSTRAINT pk_audit_logs PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )
    # asyncpg runs one statement per execute.
    op.execute("CREATE TABLE audit_logs_default PARTITION OF audit_logs DEFAULT")
    op.execute("CREATE INDEX ix_audit_logs_tenant_id_created_at ON audit_logs (tenant_id, created_at DESC)")
    op.execute("CREATE INDEX ix_audit_logs_entity ON audit_logs (entity_type, entity_id)")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ensure_audit_partition(month date) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE
            start_d date := date_trunc('month', month)::date;
            end_d   date := (date_trunc('month', month) + interval '1 month')::date;
            part    text := 'audit_logs_' || to_char(start_d, 'YYYYMM');
        BEGIN
            IF to_regclass(part) IS NULL THEN
                EXECUTE format('CREATE TABLE %I PARTITION OF audit_logs FOR VALUES FROM (%L) TO (%L)',
                               part, start_d, end_d);
            END IF;
        END $$
        """
    )
    op.execute(
        "SELECT ensure_audit_partition((now() + (n || ' month')::interval)::date) FROM generate_series(0, 2) AS n"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_logs CASCADE")
    op.execute("DROP FUNCTION IF EXISTS ensure_audit_partition(date)")
    for table in (
        "jobs",
        "api_keys",
        "refresh_tokens",
        "user_tokens",
        "user_roles",
        "users",
        "role_permissions",
        "roles",
        "permissions",
        "tenants",
    ):
        op.drop_table(table)
    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
