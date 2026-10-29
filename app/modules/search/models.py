import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import CHAR, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import Entity

EMBEDDING_DIMENSIONS = 1536  # must match Settings.embedding_dimensions


class VendorEmbedding(Entity, Base):
    """Search vectors for the global knowledge base: one 'profile' chunk per vendor (everything known, condensed) plus
    one chunk per equipment and service fact. Public like the knowledge base itself (no tenant_id, no RLS).

    `content_sha` lets a refresh skip chunks whose text didn't change; `knowledge_version` records which version of the
    vendor the chunk was built from, so stale vendors are easy to find."""

    __tablename__ = "vendor_embeddings"
    __table_args__ = (
        UniqueConstraint("vendor_id", "kind", "ref_id", name="uq_vendor_embeddings_vendor_kind_ref"),
        Index(
            "ix_vendor_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # profile | equipment | service
    # The fact the chunk describes (vendor_equipment.id / vendor_services.id); the vendor's own id for 'profile', so
    # the unique constraint works without NULLs.
    ref_id: Mapped[uuid.UUID] = mapped_column()
    content: Mapped[str] = mapped_column(Text)
    content_sha: Mapped[str] = mapped_column(CHAR(64))
    model: Mapped[str] = mapped_column(String(80))
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    knowledge_version: Mapped[int] = mapped_column(Integer)


class SearchHistory(Entity, Base):
    """A user's searches (query, the intent used, filters, what came back), private to the organization (RLS).
    Also the cache for AI query parsing: the same query in the same organization reuses its intent."""

    __tablename__ = "search_history"
    __table_args__ = (Index("ix_search_history_tenant_user_created", "tenant_id", "user_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    query: Mapped[str] = mapped_column(String(500))
    query_key: Mapped[str] = mapped_column(CHAR(64))  # sha256 of the normalised query
    parser: Mapped[str] = mapped_column(String(10))
    intent: Mapped[dict[str, Any]] = mapped_column(default=dict)
    filters: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), default=list)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    took_ms: Mapped[int] = mapped_column(Integer, default=0)
