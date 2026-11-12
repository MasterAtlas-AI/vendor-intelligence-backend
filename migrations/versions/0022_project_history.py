"""An organization's own project history (SOW review changes 1-3, 8, 9)

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-11

Tenant-private, FORCE'd RLS: project equipment lines (from MELs), procurement records, knowledge notes, MEL rows
under review, saved searches. Attribute definitions: shared defaults (tenant_id NULL, readable by all, written only
in platform mode) plus an organization's own. Also: project kind / year / location, structured attributes on
catalogue products, and users' department.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"
VERIFICATION = (
    "('verified', 'historical', 'vendor_provided', 'public', 'ai_extracted', 'user_entered', 'archived')"
)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _entity() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
    ]


def _tenant(table: str, nullable: bool = False) -> list[sa.Column | sa.Constraint]:
    return [
        sa.Column("tenant_id", sa.Uuid(), nullable=nullable),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="CASCADE"
        ),
    ]


def _fk(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.id"], name=f"fk_{table}_{column}_{target}", ondelete=ondelete
    )


def _private(table: str, *, nullable_tenant: bool = False) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    match = "IS NOT DISTINCT FROM" if nullable_tenant else "="
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table}
          USING (tenant_id {match} {CURRENT_TENANT} OR {IS_PLATFORM})
          WITH CHECK (tenant_id {match} {CURRENT_TENANT} OR {IS_PLATFORM})
        """
    )


def upgrade() -> None:
    # ---------------------------------------------------------------- existing tables
    op.add_column(
        "tenant_projects", sa.Column("kind", sa.String(12), server_default="sourcing", nullable=False)
    )
    op.add_column("tenant_projects", sa.Column("year", sa.SmallInteger(), nullable=True))
    op.add_column("tenant_projects", sa.Column("location", sa.String(10), nullable=True))
    op.create_check_constraint("kind", "tenant_projects", "kind IN ('sourcing', 'historical')")
    op.create_check_constraint(
        "location", "tenant_projects", "location IS NULL OR location IN ('offshore', 'onshore')"
    )
    op.add_column("vendor_equipment", sa.Column("attributes", JSONB, server_default="{}", nullable=False))
    op.add_column("users", sa.Column("department", sa.String(30), nullable=True))

    # ---------------------------------------------------------------- project equipment
    t = "project_equipment"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_name", sa.String(300), nullable=False),
        sa.Column("manufacturer_id", sa.Uuid(), nullable=True),
        sa.Column("manufacturer_name", sa.String(300), nullable=False),
        sa.Column("tag", sa.String(120), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("service", sa.String(300), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("attributes", JSONB, nullable=False),
        sa.Column("standards", postgresql.ARRAY(sa.String(80)), nullable=False),
        sa.Column("specs_text", sa.Text(), nullable=False),
        sa.Column("department", sa.String(30), nullable=True),
        sa.Column("verification", sa.String(20), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("source_upload_id", sa.Uuid(), nullable=True),
        sa.Column("source_ref", sa.String(200), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        *_entity(),
        _fk(t, "project_id", "tenant_projects", "CASCADE"),
        _fk(t, "category_id", "equipment_categories", "SET NULL"),
        _fk(t, "vendor_id", "vendors", "SET NULL"),
        sa.ForeignKeyConstraint(
            ["manufacturer_id"], ["vendors.id"], name=f"fk_{t}_manufacturer_id_vendors", ondelete="SET NULL"
        ),
        _fk(t, "source_upload_id", "uploads", "SET NULL"),
        sa.CheckConstraint(f"verification IN {VERIFICATION}", name=f"ck_{t}_verification"),
        sa.CheckConstraint(
            "origin IN ('mel', 'po', 'datasheet', 'tbe', 'vendor_document', 'manual', 'extraction')",
            name=f"ck_{t}_origin",
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
    )
    op.create_index("ix_project_equipment_tenant_project", t, ["tenant_id", "project_id"])
    op.create_index("ix_project_equipment_tenant_category", t, ["tenant_id", "category_id"])
    op.create_index("ix_project_equipment_tenant_vendor", t, ["tenant_id", "vendor_id"])
    op.create_index("ix_project_equipment_tenant_id", t, ["tenant_id"])
    _private(t)

    # ---------------------------------------------------------------- procurement records
    t = "procurement_records"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("equipment_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_name", sa.String(300), nullable=False),
        sa.Column("po_number", sa.String(80), nullable=False),
        sa.Column("po_date", sa.Date(), nullable=True),
        sa.Column("promised_date", sa.Date(), nullable=True),
        sa.Column("delivered_date", sa.Date(), nullable=True),
        sa.Column("lead_time_weeks", sa.SmallInteger(), nullable=True),
        sa.Column("manufacturing_weeks", sa.SmallInteger(), nullable=True),
        sa.Column("amount", sa.Numeric(16, 2), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("on_time", sa.Boolean(), nullable=True),
        sa.Column("performance_rating", sa.SmallInteger(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("department", sa.String(30), nullable=True),
        sa.Column("verification", sa.String(20), nullable=False),
        *_entity(),
        _fk(t, "project_id", "tenant_projects", "CASCADE"),
        _fk(t, "equipment_id", "project_equipment", "SET NULL"),
        _fk(t, "vendor_id", "vendors", "SET NULL"),
        sa.CheckConstraint(
            "status IN ('ordered', 'manufacturing', 'shipped', 'delivered', 'cancelled')",
            name=f"ck_{t}_status",
        ),
        sa.CheckConstraint(
            "performance_rating IS NULL OR performance_rating BETWEEN 1 AND 5", name=f"ck_{t}_rating"
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
    )
    op.create_index("ix_procurement_records_tenant_vendor", t, ["tenant_id", "vendor_id"])
    op.create_index("ix_procurement_records_tenant_project", t, ["tenant_id", "project_id"])
    op.create_index("ix_procurement_records_tenant_id", t, ["tenant_id"])
    _private(t)

    # ---------------------------------------------------------------- knowledge notes
    t = "knowledge_notes"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("department", sa.String(30), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("equipment_id", sa.Uuid(), nullable=True),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("verification", sa.String(20), nullable=False),
        *_entity(),
        _fk(t, "project_id", "tenant_projects", "CASCADE"),
        _fk(t, "vendor_id", "vendors", "SET NULL"),
        _fk(t, "equipment_id", "project_equipment", "CASCADE"),
        _fk(t, "category_id", "equipment_categories", "SET NULL"),
        sa.CheckConstraint(
            "kind IN ('lesson_learned', 'operating_feedback', 'maintenance', 'spares', 'performance', 'other')",
            name=f"ck_{t}_kind",
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
    )
    op.create_index("ix_knowledge_notes_tenant_vendor", t, ["tenant_id", "vendor_id"])
    op.create_index("ix_knowledge_notes_tenant_id", t, ["tenant_id"])
    _private(t)

    # ---------------------------------------------------------------- attribute definitions
    t = "equipment_attributes"
    op.create_table(
        t,
        *_tenant(t, nullable=True),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("data_type", sa.String(10), nullable=False),
        sa.Column("unit", sa.String(20), nullable=False),
        sa.Column("quantity", sa.String(20), nullable=False),
        sa.Column("synonyms", postgresql.ARRAY(sa.String(80)), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False),
        *_entity(),
        _fk(t, "category_id", "equipment_categories", "CASCADE"),
        sa.CheckConstraint("data_type IN ('number', 'text')", name=f"ck_{t}_data_type"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
    )
    op.create_index("ix_equipment_attributes_tenant_key", t, ["tenant_id", "key"])
    op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {t}
          USING (tenant_id IS NULL OR tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
          WITH CHECK (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
        """
    )

    # ---------------------------------------------------------------- MEL rows under review
    t = "mel_rows"
    op.create_table(
        t,
        *_tenant(t),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column("parsed", JSONB, nullable=False),
        sa.Column("issues", postgresql.ARRAY(sa.String(200)), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("equipment_id", sa.Uuid(), nullable=True),
        *_entity(),
        _fk(t, "upload_id", "uploads", "CASCADE"),
        _fk(t, "project_id", "tenant_projects", "CASCADE"),
        _fk(t, "equipment_id", "project_equipment", "SET NULL"),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name=f"ck_{t}_status"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
    )
    op.create_index("ix_mel_rows_upload_status", t, ["upload_id", "status"])
    op.create_index("ix_mel_rows_tenant_id", t, ["tenant_id"])
    _private(t)

    # ---------------------------------------------------------------- saved searches
    t = "saved_searches"
    op.create_table(
        t,
        *_tenant(t, nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("query", sa.String(500), nullable=False),
        sa.Column("filters", JSONB, nullable=False),
        *_entity(),
        _fk(t, "user_id", "users", "CASCADE"),
        sa.CheckConstraint("kind IN ('vendors', 'equipment')", name=f"ck_{t}_kind"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
    )
    op.create_index("ix_saved_searches_tenant_user", t, ["tenant_id", "user_id"])
    _private(t, nullable_tenant=True)


def downgrade() -> None:
    for table in (
        "saved_searches",
        "mel_rows",
        "equipment_attributes",
        "knowledge_notes",
        "procurement_records",
        "project_equipment",
    ):
        op.drop_table(table)
    op.drop_column("users", "department")
    op.drop_column("vendor_equipment", "attributes")
    op.drop_constraint(op.f("ck_tenant_projects_location"), "tenant_projects", type_="check")
    op.drop_constraint(op.f("ck_tenant_projects_kind"), "tenant_projects", type_="check")
    op.drop_column("tenant_projects", "location")
    op.drop_column("tenant_projects", "year")
    op.drop_column("tenant_projects", "kind")
