"""Knowledge base: taxonomy, global vendors and their facts, tenant vendor overlay

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-29

Global tables (vendors, facts, taxonomy) have no tenant_id and no RLS: every tenant reads them, and writes go
through the curator approval workflow in the application. The tenant overlay (tenant_vendor_links) is
tenant-private and gets the same FORCE'd RLS policy as the other tenant tables (see 0003).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = {
    "vendor_status": ("active", "inactive", "retired"),
    "fact_status": ("approved", "retired"),
    "location_type": ("hq", "factory", "service_center", "sales", "rnd", "other"),
    "source_kind": ("web", "document", "excel", "manual", "ai"),
    "avl_status": ("approved", "conditional", "not_approved", "blacklisted", "unassessed"),
}

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"


def upgrade() -> None:
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "certification_types",
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("issuer", sa.String(length=120), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("synonyms", sa.ARRAY(sa.String(length=120)), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_certification_types")),
        sa.UniqueConstraint("code", name=op.f("uq_certification_types_code")),
    )
    op.create_table(
        "equipment_categories",
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("standards", sa.ARRAY(sa.String(length=80)), nullable=False),
        sa.Column("code", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False),
        sa.Column("synonyms", sa.ARRAY(sa.String(length=120)), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["equipment_categories.id"],
            name=op.f("fk_equipment_categories_parent_id_equipment_categories"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equipment_categories")),
        sa.UniqueConstraint("code", name=op.f("uq_equipment_categories_code")),
    )
    op.create_index(
        "ix_equipment_categories_code_prefix",
        "equipment_categories",
        ["code"],
        unique=False,
        postgresql_ops={"code": "text_pattern_ops"},
    )
    op.create_index(
        op.f("ix_equipment_categories_parent_id"), "equipment_categories", ["parent_id"], unique=False
    )
    op.create_table(
        "industries",
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("synonyms", sa.ARRAY(sa.String(length=120)), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_industries")),
        sa.UniqueConstraint("code", name=op.f("uq_industries_code")),
    )
    op.create_table(
        "service_categories",
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("code", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False),
        sa.Column("synonyms", sa.ARRAY(sa.String(length=120)), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["service_categories.id"],
            name=op.f("fk_service_categories_parent_id_service_categories"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_categories")),
        sa.UniqueConstraint("code", name=op.f("uq_service_categories_code")),
    )
    op.create_index(
        "ix_service_categories_code_prefix",
        "service_categories",
        ["code"],
        unique=False,
        postgresql_ops={"code": "text_pattern_ops"},
    )
    op.create_index(
        op.f("ix_service_categories_parent_id"), "service_categories", ["parent_id"], unique=False
    )
    op.create_table(
        "vendors",
        sa.Column("legal_name", sa.String(length=300), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=300), nullable=False),
        sa.Column("website", sa.String(length=500), nullable=True),
        sa.Column("hq_country", sa.CHAR(length=2), nullable=True),
        sa.Column("hq_city", sa.String(length=120), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("active", "inactive", "retired", name="vendor_status", create_type=False),
            nullable=False,
        ),
        sa.Column("parent_vendor_id", sa.Uuid(), nullable=True),
        sa.Column("data_quality_score", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("knowledge_version", sa.Integer(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('simple', coalesce(display_name, '') || ' ' || coalesce(legal_name, '')), 'A') || setweight(to_tsvector('english', coalesce(description, '')), 'C')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["parent_vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendors_parent_vendor_id_vendors"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendors")),
    )
    op.create_index("ix_vendors_hq_country", "vendors", ["hq_country"], unique=False)
    op.create_index(op.f("ix_vendors_normalized_name"), "vendors", ["normalized_name"], unique=False)
    op.create_index(
        "ix_vendors_normalized_name_trgm",
        "vendors",
        ["normalized_name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )
    op.create_index("ix_vendors_search_tsv", "vendors", ["search_tsv"], unique=False, postgresql_using="gin")
    op.create_table(
        "tenant_vendor_links",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "avl_status",
            postgresql.ENUM(
                "approved",
                "conditional",
                "not_approved",
                "blacklisted",
                "unassessed",
                name="avl_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("internal_vendor_code", sa.String(length=80), nullable=True),
        sa.Column("tags", sa.ARRAY(sa.String(length=60)), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("custom_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_vendor_links_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_tenant_vendor_links_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_vendor_links")),
        sa.UniqueConstraint(
            "tenant_id", "vendor_id", name=op.f("uq_tenant_vendor_links_tenant_id_vendor_id")
        ),
    )
    op.create_index(
        op.f("ix_tenant_vendor_links_tenant_id"), "tenant_vendor_links", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_tenant_vendor_links_vendor_id"), "tenant_vendor_links", ["vendor_id"], unique=False
    )
    op.create_table(
        "vendor_aliases",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("alias", sa.String(length=300), nullable=False),
        sa.Column("normalized_alias", sa.String(length=300), nullable=False),
        sa.Column("alias_type", sa.String(length=20), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_aliases_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_aliases")),
        sa.UniqueConstraint(
            "vendor_id", "normalized_alias", name=op.f("uq_vendor_aliases_vendor_id_normalized_alias")
        ),
    )
    op.create_index(
        "ix_vendor_aliases_normalized_alias_trgm",
        "vendor_aliases",
        ["normalized_alias"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_alias": "gin_trgm_ops"},
    )
    op.create_index(op.f("ix_vendor_aliases_vendor_id"), "vendor_aliases", ["vendor_id"], unique=False)
    op.create_table(
        "vendor_certifications",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("certification_type_id", sa.Uuid(), nullable=False),
        sa.Column("certificate_number", sa.String(length=120), nullable=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("approved", "retired", name="fact_status", create_type=False),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["certification_type_id"],
            ["certification_types.id"],
            name=op.f("fk_vendor_certifications_certification_type_id_certification_types"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_certifications_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_certifications")),
    )
    op.create_index(
        "ix_vendor_certifications_type_vendor",
        "vendor_certifications",
        ["certification_type_id", "vendor_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vendor_certifications_vendor_id"), "vendor_certifications", ["vendor_id"], unique=False
    )
    op.create_table(
        "vendor_domains",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("domain", postgresql.CITEXT(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_domains_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_domains")),
        sa.UniqueConstraint("domain", name=op.f("uq_vendor_domains_domain")),
    )
    op.create_index(op.f("ix_vendor_domains_vendor_id"), "vendor_domains", ["vendor_id"], unique=False)
    op.create_table(
        "vendor_equipment",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("model_series", sa.String(length=200), nullable=True),
        sa.Column("standards", sa.ARRAY(sa.String(length=80)), nullable=False),
        sa.Column("specs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("applications", sa.ARRAY(sa.String(length=120)), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("approved", "retired", name="fact_status", create_type=False),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["equipment_categories.id"],
            name=op.f("fk_vendor_equipment_category_id_equipment_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_equipment_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_equipment")),
    )
    op.create_index(
        "ix_vendor_equipment_category_vendor", "vendor_equipment", ["category_id", "vendor_id"], unique=False
    )
    op.create_index(op.f("ix_vendor_equipment_vendor_id"), "vendor_equipment", ["vendor_id"], unique=False)
    op.create_table(
        "vendor_industries",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("industry_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["industry_id"],
            ["industries.id"],
            name=op.f("fk_vendor_industries_industry_id_industries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_industries_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("vendor_id", "industry_id", name=op.f("pk_vendor_industries")),
    )
    op.create_index(
        op.f("ix_vendor_industries_industry_id"), "vendor_industries", ["industry_id"], unique=False
    )
    op.create_table(
        "vendor_locations",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "location_type",
            postgresql.ENUM(
                "hq",
                "factory",
                "service_center",
                "sales",
                "rnd",
                "other",
                name="location_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("country", sa.CHAR(length=2), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM("approved", "retired", name="fact_status", create_type=False),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_locations_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_locations")),
    )
    op.create_index(op.f("ix_vendor_locations_country"), "vendor_locations", ["country"], unique=False)
    op.create_index(op.f("ix_vendor_locations_vendor_id"), "vendor_locations", ["vendor_id"], unique=False)
    op.create_table(
        "vendor_projects",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("project_name", sa.String(length=300), nullable=False),
        sa.Column("client", sa.String(length=200), nullable=True),
        sa.Column("operator", sa.String(length=200), nullable=True),
        sa.Column("facility_type", sa.String(length=60), nullable=True),
        sa.Column("country", sa.CHAR(length=2), nullable=True),
        sa.Column("year", sa.SmallInteger(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("approved", "retired", name="fact_status", create_type=False),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_projects_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_projects")),
    )
    op.create_index(op.f("ix_vendor_projects_vendor_id"), "vendor_projects", ["vendor_id"], unique=False)
    op.create_table(
        "vendor_services",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("equipment_category_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("regions", sa.ARRAY(sa.String(length=60)), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("approved", "retired", name="fact_status", create_type=False),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["service_categories.id"],
            name=op.f("fk_vendor_services_category_id_service_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_category_id"],
            ["equipment_categories.id"],
            name=op.f("fk_vendor_services_equipment_category_id_equipment_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_services_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_services")),
    )
    op.create_index(
        "ix_vendor_services_category_vendor", "vendor_services", ["category_id", "vendor_id"], unique=False
    )
    op.create_index(op.f("ix_vendor_services_vendor_id"), "vendor_services", ["vendor_id"], unique=False)
    op.create_table(
        "vendor_sources",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "web", "document", "excel", "manual", "ai", name="source_kind", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reliability", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_sources_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_sources")),
    )
    op.create_index(op.f("ix_vendor_sources_vendor_id"), "vendor_sources", ["vendor_id"], unique=False)
    op.create_table(
        "fact_sources",
        sa.Column("fact_table", sa.String(length=40), nullable=False),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["vendor_sources.id"],
            name=op.f("fk_fact_sources_source_id_vendor_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fact_sources")),
    )
    op.create_index("ix_fact_sources_fact", "fact_sources", ["fact_table", "fact_id"], unique=False)
    op.create_index(op.f("ix_fact_sources_source_id"), "fact_sources", ["source_id"], unique=False)

    op.execute("ALTER TABLE tenant_vendor_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_vendor_links FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON tenant_vendor_links
          USING (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
          WITH CHECK (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_fact_sources_source_id"), table_name="fact_sources")
    op.drop_index("ix_fact_sources_fact", table_name="fact_sources")
    op.drop_table("fact_sources")
    op.drop_index(op.f("ix_vendor_sources_vendor_id"), table_name="vendor_sources")
    op.drop_table("vendor_sources")
    op.drop_index(op.f("ix_vendor_services_vendor_id"), table_name="vendor_services")
    op.drop_index("ix_vendor_services_category_vendor", table_name="vendor_services")
    op.drop_table("vendor_services")
    op.drop_index(op.f("ix_vendor_projects_vendor_id"), table_name="vendor_projects")
    op.drop_table("vendor_projects")
    op.drop_index(op.f("ix_vendor_locations_vendor_id"), table_name="vendor_locations")
    op.drop_index(op.f("ix_vendor_locations_country"), table_name="vendor_locations")
    op.drop_table("vendor_locations")
    op.drop_index(op.f("ix_vendor_industries_industry_id"), table_name="vendor_industries")
    op.drop_table("vendor_industries")
    op.drop_index(op.f("ix_vendor_equipment_vendor_id"), table_name="vendor_equipment")
    op.drop_index("ix_vendor_equipment_category_vendor", table_name="vendor_equipment")
    op.drop_table("vendor_equipment")
    op.drop_index(op.f("ix_vendor_domains_vendor_id"), table_name="vendor_domains")
    op.drop_table("vendor_domains")
    op.drop_index(op.f("ix_vendor_certifications_vendor_id"), table_name="vendor_certifications")
    op.drop_index("ix_vendor_certifications_type_vendor", table_name="vendor_certifications")
    op.drop_table("vendor_certifications")
    op.drop_index(op.f("ix_vendor_aliases_vendor_id"), table_name="vendor_aliases")
    op.drop_index(
        "ix_vendor_aliases_normalized_alias_trgm",
        table_name="vendor_aliases",
        postgresql_using="gin",
        postgresql_ops={"normalized_alias": "gin_trgm_ops"},
    )
    op.drop_table("vendor_aliases")
    op.drop_index(op.f("ix_tenant_vendor_links_vendor_id"), table_name="tenant_vendor_links")
    op.drop_index(op.f("ix_tenant_vendor_links_tenant_id"), table_name="tenant_vendor_links")
    op.drop_table("tenant_vendor_links")
    op.drop_index("ix_vendors_search_tsv", table_name="vendors", postgresql_using="gin")
    op.drop_index(
        "ix_vendors_normalized_name_trgm",
        table_name="vendors",
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )
    op.drop_index(op.f("ix_vendors_normalized_name"), table_name="vendors")
    op.drop_index("ix_vendors_hq_country", table_name="vendors")
    op.drop_table("vendors")
    op.drop_index(op.f("ix_service_categories_parent_id"), table_name="service_categories")
    op.drop_index(
        "ix_service_categories_code_prefix",
        table_name="service_categories",
        postgresql_ops={"code": "text_pattern_ops"},
    )
    op.drop_table("service_categories")
    op.drop_table("industries")
    op.drop_index(op.f("ix_equipment_categories_parent_id"), table_name="equipment_categories")
    op.drop_index(
        "ix_equipment_categories_code_prefix",
        table_name="equipment_categories",
        postgresql_ops={"code": "text_pattern_ops"},
    )
    op.drop_table("equipment_categories")
    op.drop_table("certification_types")

    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
