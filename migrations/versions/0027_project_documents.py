"""Project documents: drawings, correspondence, schedules and the like, beside the equipment history

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-12

SOW 3.13 / 3.14 / 3.17 / 14.6. Tenant-private with FORCE'd row-level security, like the rest of project history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"
TYPES = (
    "'drawing', 'datasheet', 'specification', 'correspondence', 'schedule', 'report', 'certificate', 'other'"
)
T = "project_documents"


def _fk(column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.id"], name=f"fk_{T}_{column}_{target}", ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        T,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("equipment_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("doc_type", sa.String(20), nullable=False),
        sa.Column("doc_number", sa.String(120), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("revision", sa.String(20), nullable=False),
        sa.Column("doc_date", sa.Date(), nullable=True),
        sa.Column("department", sa.String(30), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=True),
        _fk("tenant_id", "tenants", "CASCADE"),
        _fk("project_id", "tenant_projects", "CASCADE"),
        _fk("equipment_id", "project_equipment", "SET NULL"),
        _fk("vendor_id", "vendors", "SET NULL"),
        sa.CheckConstraint(f"doc_type IN ({TYPES})", name="doc_type"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{T}"),
    )
    op.create_index("ix_project_documents_tenant_project", T, ["tenant_id", "project_id"])
    op.create_index("ix_project_documents_equipment", T, ["equipment_id"])
    op.create_index("ix_project_documents_tenant_id", T, ["tenant_id"])
    op.execute(f"ALTER TABLE {T} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {T} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {T}
          USING (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
          WITH CHECK (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
        """
    )


def downgrade() -> None:
    op.drop_table(T)
