import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import Entity


class AssistantConversation(Entity, Base):
    """A user's saved assistant chat, private to that user within the organization (RLS on the tenant, and every
    query filters on the user). Platform staff outside an organization have tenant_id NULL, like search history."""

    __tablename__ = "assistant_conversations"
    __table_args__ = (Index("ix_assistant_conversations_user_updated", "tenant_id", "user_id", "updated_at"),)

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))


class AssistantMessage(Entity, Base):
    """One message of a saved chat. Assistant messages keep the whole reply (cited vendors, actions, follow-ups) so a
    reopened chat looks exactly as it did."""

    __tablename__ = "assistant_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="role"),
        Index("ix_assistant_messages_conversation_created", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE")
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    reply: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
