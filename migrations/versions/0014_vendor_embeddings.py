"""Search vectors for vendors (Phase 4)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-29

Public knowledge-base data like vendors themselves: no tenant_id and no RLS. Only background jobs write it. The
`vector` extension was enabled in 0001; the HNSW index answers cosine-similarity queries.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vendor_embeddings",
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("ref_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha", sa.CHAR(length=64), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("knowledge_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_embeddings_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_embeddings")),
        sa.UniqueConstraint("vendor_id", "kind", "ref_id", name="uq_vendor_embeddings_vendor_kind_ref"),
    )
    op.create_index(op.f("ix_vendor_embeddings_vendor_id"), "vendor_embeddings", ["vendor_id"], unique=False)
    op.create_index(
        "ix_vendor_embeddings_hnsw",
        "vendor_embeddings",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_vendor_embeddings_hnsw", table_name="vendor_embeddings")
    op.drop_index(op.f("ix_vendor_embeddings_vendor_id"), table_name="vendor_embeddings")
    op.drop_table("vendor_embeddings")
