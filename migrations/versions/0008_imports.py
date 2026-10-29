"""Uploads (files stored in Postgres) and import report rows

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-29

Both tables are tenant-private with FORCE'd RLS. Unlike the other tenant tables, tenant_id may be NULL (uploads
by platform curators who aren't acting as a tenant), so the policy matches with IS NOT DISTINCT FROM: tenant-less
rows are visible only to tenant-less sessions.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = {
    "upload_kind": ("spreadsheet", "pdf"),
    "import_row_outcome": (
        "matched",
        "created",
        "proposed",
        "already_proposed",
        "duplicate_in_file",
        "ambiguous",
        "invalid",
    ),
}

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"
TABLES = ("uploads", "import_rows")


def _entity_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
    ]


def upgrade() -> None:
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "uploads",
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column(
            "kind",
            postgresql.ENUM(*ENUMS["upload_kind"], name="upload_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        *_entity_columns(),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_uploads_tenant_id_tenants"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_uploads")),
    )
    op.create_index("ix_uploads_tenant_id_created_at", "uploads", ["tenant_id", "created_at"], unique=False)

    op.create_table(
        "import_rows",
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=True),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column(
            "outcome",
            postgresql.ENUM(*ENUMS["import_row_outcome"], name="import_row_outcome", create_type=False),
            nullable=False,
        ),
        sa.Column("vendor_name", sa.String(length=300), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("overlay", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("messages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_entity_columns(),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_import_rows_tenant_id_tenants"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_import_rows_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["upload_id"], ["uploads.id"], name=op.f("fk_import_rows_upload_id_uploads"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"], ["vendors.id"], name=op.f("fk_import_rows_vendor_id_vendors"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["knowledge_proposals.id"],
            name=op.f("fk_import_rows_proposal_id_knowledge_proposals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_rows")),
    )
    op.create_index("ix_import_rows_job_id_row_number", "import_rows", ["job_id", "row_number"], unique=False)
    op.create_index(
        "ix_import_rows_pending_link",
        "import_rows",
        ["proposal_id"],
        unique=False,
        postgresql_where=sa.text("vendor_id IS NULL AND proposal_id IS NOT NULL"),
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (tenant_id IS NOT DISTINCT FROM {CURRENT_TENANT} OR {IS_PLATFORM})
              WITH CHECK (tenant_id IS NOT DISTINCT FROM {CURRENT_TENANT} OR {IS_PLATFORM})
            """
        )


def downgrade() -> None:
    op.drop_index("ix_import_rows_pending_link", table_name="import_rows")
    op.drop_index("ix_import_rows_job_id_row_number", table_name="import_rows")
    op.drop_table("import_rows")
    op.drop_index("ix_uploads_tenant_id_created_at", table_name="uploads")
    op.drop_table("uploads")

    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
