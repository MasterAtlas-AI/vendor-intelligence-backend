"""Equipment status on project equipment lines; inspection (QA/QC) and project-update notes

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-12

SOW 14.8: project management needs where each equipment line stands (planned ... in service ... decommissioned).
SOW 3.15 / 10.2: inspection and QA/QC records, and project-management updates, kept as notes of their own kind.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'planned', 'ordered', 'delivered', 'installed', 'in_service', 'standby', 'decommissioned'"
OLD_KINDS = "'lesson_learned', 'operating_feedback', 'maintenance', 'spares', 'performance', 'other'"
NEW_KINDS = (
    "'lesson_learned', 'operating_feedback', 'maintenance', 'spares', 'performance', 'inspection', "
    "'project_update', 'other'"
)


# 0022 named it explicitly inside create_table, so the naming convention prefixed it twice.
_DROP_KIND = (
    "ALTER TABLE knowledge_notes DROP CONSTRAINT IF EXISTS ck_knowledge_notes_ck_knowledge_notes_kind, "
    "DROP CONSTRAINT IF EXISTS ck_knowledge_notes_kind"
)


def upgrade() -> None:
    op.add_column("project_equipment", sa.Column("status", sa.String(20), nullable=True))
    op.create_check_constraint("status", "project_equipment", f"status IS NULL OR status IN ({STATUSES})")
    op.execute(_DROP_KIND)
    op.create_check_constraint("kind", "knowledge_notes", f"kind IN ({NEW_KINDS})")


def downgrade() -> None:
    op.execute("UPDATE knowledge_notes SET kind = 'other' WHERE kind IN ('inspection', 'project_update')")
    op.execute(_DROP_KIND)
    op.create_check_constraint("kind", "knowledge_notes", f"kind IN ({OLD_KINDS})")
    op.drop_constraint(op.f("ck_project_equipment_status"), "project_equipment", type_="check")
    op.drop_column("project_equipment", "status")
