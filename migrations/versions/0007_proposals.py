"""Knowledge proposals, items and approval history; audit inserts for platform-level events

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-29

Proposals are global (no tenant_id, no RLS): curators review them across tenants, and the API only shows a
tenant the proposals that originated from it.

Curator decisions are platform-level audit events (tenant_id NULL), but curators don't run in platform mode, so
the audit_logs policy is split: reads stay tenant-scoped, inserts may also write tenant-less rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = {
    "proposal_kind": (
        "new_vendor",
        "update_vendor",
    ),
    "proposal_origin": (
        "manual",
        "excel",
        "pdf",
        "ai_vendor_discovery",
        "ai_equipment_discovery",
        "suggestion",
    ),
    "proposal_status": (
        "pending_review",
        "needs_info",
        "approved",
        "partially_approved",
        "rejected",
        "auto_approved",
    ),
    "proposal_item_action": (
        "create_vendor",
        "update_vendor_field",
        "add_domain",
        "add_alias",
        "add_location",
        "add_industry",
        "add_equipment",
        "add_service",
        "add_certification",
        "add_project",
    ),
    "proposal_item_status": (
        "pending",
        "rejected",
        "applied",
    ),
    "approval_action": (
        "approve",
        "reject",
        "request_info",
        "auto_approve",
        "edit",
    ),
}

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"


def upgrade() -> None:
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "knowledge_proposals",
        sa.Column(
            "kind",
            postgresql.ENUM("new_vendor", "update_vendor", name="proposal_kind", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "origin",
            postgresql.ENUM(
                "manual",
                "excel",
                "pdf",
                "ai_vendor_discovery",
                "ai_equipment_discovery",
                "suggestion",
                name="proposal_origin",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending_review",
                "needs_info",
                "approved",
                "partially_approved",
                "rejected",
                "auto_approved",
                name="proposal_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("subject_vendor_id", sa.Uuid(), nullable=True),
        sa.Column("origin_tenant_id", sa.Uuid(), nullable=True),
        sa.Column("origin_user_id", sa.Uuid(), nullable=True),
        sa.Column("origin_job_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("assigned_to", sa.Uuid(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["origin_job_id"],
            ["jobs.id"],
            name=op.f("fk_knowledge_proposals_origin_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["origin_tenant_id"],
            ["tenants.id"],
            name=op.f("fk_knowledge_proposals_origin_tenant_id_tenants"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["subject_vendor_id"],
            ["vendors.id"],
            name=op.f("fk_knowledge_proposals_subject_vendor_id_vendors"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_proposals")),
    )
    op.create_index(
        "ix_knowledge_proposals_origin_tenant_id_created_at",
        "knowledge_proposals",
        ["origin_tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_proposals_status_created_at",
        "knowledge_proposals",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_proposals_subject_vendor_id"),
        "knowledge_proposals",
        ["subject_vendor_id"],
        unique=False,
    )
    op.create_table(
        "proposal_items",
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False),
        sa.Column(
            "action",
            postgresql.ENUM(
                "create_vendor",
                "update_vendor_field",
                "add_domain",
                "add_alias",
                "add_location",
                "add_industry",
                "add_equipment",
                "add_service",
                "add_certification",
                "add_project",
                name="proposal_item_action",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM("pending", "rejected", "applied", name="proposal_item_status", create_type=False),
            nullable=False,
        ),
        sa.Column("applied_table", sa.String(length=40), nullable=True),
        sa.Column("applied_entity_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["knowledge_proposals.id"],
            name=op.f("fk_proposal_items_proposal_id_knowledge_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposal_items")),
    )
    op.create_index(op.f("ix_proposal_items_proposal_id"), "proposal_items", ["proposal_id"], unique=False)
    op.create_table(
        "approval_history",
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_item_id", sa.Uuid(), nullable=True),
        sa.Column(
            "action",
            postgresql.ENUM(
                "approve",
                "reject",
                "request_info",
                "auto_approve",
                "edit",
                name="approval_action",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["knowledge_proposals.id"],
            name=op.f("fk_approval_history_proposal_id_knowledge_proposals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_item_id"],
            ["proposal_items.id"],
            name=op.f("fk_approval_history_proposal_item_id_proposal_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_history")),
    )
    op.create_index(
        op.f("ix_approval_history_proposal_id"), "approval_history", ["proposal_id"], unique=False
    )
    op.create_index(
        op.f("ix_approval_history_proposal_item_id"), "approval_history", ["proposal_item_id"], unique=False
    )

    op.execute("DROP POLICY tenant_isolation ON audit_logs")
    op.execute(
        f"CREATE POLICY audit_read ON audit_logs FOR SELECT USING (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})"
    )
    op.execute(
        f"CREATE POLICY audit_insert ON audit_logs FOR INSERT "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM} OR tenant_id IS NULL)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY audit_insert ON audit_logs")
    op.execute("DROP POLICY audit_read ON audit_logs")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON audit_logs
          USING (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
          WITH CHECK (tenant_id = {CURRENT_TENANT} OR {IS_PLATFORM})
        """
    )
    op.drop_index(op.f("ix_approval_history_proposal_item_id"), table_name="approval_history")
    op.drop_index(op.f("ix_approval_history_proposal_id"), table_name="approval_history")
    op.drop_table("approval_history")
    op.drop_index(op.f("ix_proposal_items_proposal_id"), table_name="proposal_items")
    op.drop_table("proposal_items")
    op.drop_index(op.f("ix_knowledge_proposals_subject_vendor_id"), table_name="knowledge_proposals")
    op.drop_index("ix_knowledge_proposals_status_created_at", table_name="knowledge_proposals")
    op.drop_index("ix_knowledge_proposals_origin_tenant_id_created_at", table_name="knowledge_proposals")
    op.drop_table("knowledge_proposals")

    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
