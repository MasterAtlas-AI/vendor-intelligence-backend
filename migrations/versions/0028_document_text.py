"""Project documents keep the text read from their file, for search across projects

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-12

SOW H.4 / 25.4: the words of reports, letters and datasheets, read on the server (no AI), with a full-text index.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "project_documents"


def upgrade() -> None:
    op.add_column(T, sa.Column("text_content", sa.Text(), nullable=True))
    op.add_column(
        T, sa.Column("text_chars", sa.Integer(), server_default="-1", nullable=False)
    )  # -1: not read yet
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{T}_text_search ON {T} USING gin "
        "(to_tsvector('english', coalesce(title, '') || ' ' || coalesce(doc_number, '') || ' ' "
        "|| coalesce(text_content, '')))"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS ix_{T}_text_search")
    op.drop_column(T, "text_chars")
    op.drop_column(T, "text_content")
