"""Purchase and material requisitions on procurement records

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-12

SOW 3.6 / 3.7: a requisition is recorded like a PO, with record_type 'pr' (purchase requisition) or 'mr' (material
requisition), and two stages before an order exists: raised, approved.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0022 named the status check inside create_table, so the naming convention may have prefixed it twice.
_DROP = (
    "ALTER TABLE procurement_records "
    "DROP CONSTRAINT IF EXISTS ck_procurement_records_ck_procurement_records_status, "
    "DROP CONSTRAINT IF EXISTS ck_procurement_records_status, "
    "DROP CONSTRAINT IF EXISTS ck_procurement_records_record_type"
)
_STATUSES = "'ordered', 'manufacturing', 'shipped', 'delivered', 'cancelled'"


def upgrade() -> None:
    op.execute(_DROP)
    op.create_check_constraint(
        "record_type", "procurement_records", "record_type IN ('pr', 'mr', 'po', 'quotation')"
    )
    op.create_check_constraint(
        "status", "procurement_records", f"status IN ('raised', 'approved', {_STATUSES})"
    )


def downgrade() -> None:
    # procurement_records forces row-level security: platform context so the UPDATE sees every row.
    op.execute("SELECT set_config('app.is_platform', 'on', true)")
    op.execute("UPDATE procurement_records SET record_type = 'po' WHERE record_type IN ('pr', 'mr')")
    op.execute("UPDATE procurement_records SET status = 'ordered' WHERE status IN ('raised', 'approved')")
    op.execute(_DROP)
    op.create_check_constraint("record_type", "procurement_records", "record_type IN ('po', 'quotation')")
    op.create_check_constraint("status", "procurement_records", f"status IN ({_STATUSES})")
