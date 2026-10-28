"""Use the jobs table as the queue (replaces Celery/Redis)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28

Workers claim jobs with `FOR UPDATE SKIP LOCKED` and hold a renewable lease (`locked_by`, `locked_until`).
A job whose lease expires (worker crashed or was redeployed) is claimed again, up to `max_attempts`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("jobs", "celery_task_id")
    op.add_column("jobs", sa.Column("locked_by", sa.String(128)))
    op.add_column("jobs", sa.Column("locked_until", sa.DateTime(timezone=True)))
    op.add_column(
        "jobs",
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.add_column("jobs", sa.Column("max_attempts", sa.SmallInteger(), nullable=False, server_default="3"))
    # Small partial index: the claim query only ever scans unfinished jobs, oldest first.
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        ["created_at"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_claimable", table_name="jobs")
    op.drop_column("jobs", "max_attempts")
    op.drop_column("jobs", "run_after")
    op.drop_column("jobs", "locked_until")
    op.drop_column("jobs", "locked_by")
    op.add_column("jobs", sa.Column("celery_task_id", sa.String(64)))
