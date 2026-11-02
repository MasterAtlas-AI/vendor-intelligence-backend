"""Saved assistant chats

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-02

Tenant-private with FORCE'd RLS, matched like search history (0015): platform staff outside an organization have
tenant_id NULL and see only tenant-less rows. The service also filters every query on the user.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"


def _entity_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "assistant_conversations",
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        *_entity_columns(),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_assistant_conversations_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_assistant_conversations_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_conversations")),
    )  # fmt: skip
    op.create_index(
        "ix_assistant_conversations_user_updated",
        "assistant_conversations",
        ["tenant_id", "user_id", "updated_at"],
    )
    op.create_table(
        "assistant_messages",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("reply", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_entity_columns(),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_assistant_messages_role")),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["assistant_conversations.id"],
            name=op.f("fk_assistant_messages_conversation_id_assistant_conversations"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_assistant_messages_tenant_id_tenants"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_messages")),
    )  # fmt: skip
    op.create_index(
        "ix_assistant_messages_conversation_created", "assistant_messages", ["conversation_id", "created_at"]
    )
    for table in ("assistant_conversations", "assistant_messages"):
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
    op.drop_table("assistant_messages")
    op.drop_table("assistant_conversations")
