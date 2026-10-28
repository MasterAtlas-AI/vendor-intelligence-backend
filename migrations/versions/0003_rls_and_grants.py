"""Row-Level Security policies and runtime-role grants

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28

Every tenant-owned table gets a FORCE'd policy keyed on the session settings `app.tenant_id` and
`app.is_platform` (set per transaction by app.db.session). Rows with tenant_id NULL (platform staff,
system roles, platform jobs) are visible only in platform mode — except system roles, readable by all.
"""

from collections.abc import Sequence

from alembic import op

from app.db.migration_utils import app_db_role

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"

TENANT_TABLES = ["users", "user_roles", "user_tokens", "refresh_tokens", "api_keys", "jobs", "audit_logs"]


def _enable(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    role = app_db_role()
    for table in TENANT_TABLES:
        _enable(table)
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
              WITH CHECK (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
            """
        )

    # A tenant can read (not modify) its own tenant row; only platform mode manages tenants.
    _enable("tenants")
    op.execute(
        f"CREATE POLICY tenant_self_read ON tenants FOR SELECT USING (id = {CURRENT_TENANT} OR {IS_PLATFORM})"
    )
    op.execute(
        f"CREATE POLICY tenant_self_update ON tenants FOR UPDATE "
        f"USING (id = {CURRENT_TENANT} OR {IS_PLATFORM}) WITH CHECK (id = {CURRENT_TENANT} OR {IS_PLATFORM})"
    )
    op.execute(f"CREATE POLICY platform_insert ON tenants FOR INSERT WITH CHECK ({IS_PLATFORM})")
    op.execute(f"CREATE POLICY platform_delete ON tenants FOR DELETE USING ({IS_PLATFORM})")

    # System roles (tenant_id NULL) are shared; custom roles are tenant-private.
    _enable("roles")
    op.execute(
        f"CREATE POLICY roles_read ON roles FOR SELECT "
        f"USING (tenant_id IS NULL OR tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})"
    )
    op.execute(
        f"CREATE POLICY roles_write ON roles FOR ALL "
        f"USING (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})"
    )

    # Runtime role: DML only, and the audit log is append-only.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM {role}")
    op.execute(f"GRANT EXECUTE ON FUNCTION ensure_audit_partition(date) TO {role}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
    )
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {role}")


def downgrade() -> None:
    role = app_db_role()
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {role}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}")
    for table in ("roles", "tenants", *TENANT_TABLES):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    for policy in ("tenant_self_read", "tenant_self_update", "platform_insert", "platform_delete"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON tenants")
    op.execute("DROP POLICY IF EXISTS roles_read ON roles")
    op.execute("DROP POLICY IF EXISTS roles_write ON roles")
