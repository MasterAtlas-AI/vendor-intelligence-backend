"""Make audit partition maintenance callable by the runtime role

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28

0002's `ensure_audit_partition()` ran with the caller's rights, but the runtime role cannot CREATE tables
(on PG15+ the public schema is owner-only), so the scheduled maintenance task would fail. It now runs as
the owner (SECURITY DEFINER, pinned search_path) and only the runtime role may execute it.

It also refuses to create a month whose rows already landed in the DEFAULT partition (Postgres would
error); those rows simply stay in DEFAULT. Twelve months are pre-created so that never happens in practice.
"""

from collections.abc import Sequence

from alembic import op

from app.db.migration_utils import app_db_role

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONTHS_AHEAD = 12


def upgrade() -> None:
    role = app_db_role()
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ensure_audit_partition(month date) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            start_d date := date_trunc('month', month)::date;
            end_d   date := (date_trunc('month', month) + interval '1 month')::date;
            part    text := 'audit_logs_' || to_char(start_d, 'YYYYMM');
        BEGIN
            IF to_regclass(part) IS NOT NULL THEN
                RETURN;
            END IF;
            IF EXISTS (SELECT 1 FROM audit_logs_default WHERE created_at >= start_d AND created_at < end_d) THEN
                RAISE NOTICE 'audit_logs_default already holds rows for %, leaving them there', start_d;
                RETURN;
            END IF;
            EXECUTE format('CREATE TABLE %I PARTITION OF audit_logs FOR VALUES FROM (%L) TO (%L)',
                           part, start_d, end_d);
        END $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION ensure_audit_partition(date) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION ensure_audit_partition(date) TO {role}")
    op.execute(
        "SELECT ensure_audit_partition((now() + (n || ' month')::interval)::date) "
        f"FROM generate_series(0, {MONTHS_AHEAD}) AS n"
    )


def downgrade() -> None:
    # Restore 0002's invoker-rights version; partitions already created are kept (they hold audit data).
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
