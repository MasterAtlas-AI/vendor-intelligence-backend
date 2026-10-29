"""Let platform staff outside an organization (curators) run background jobs

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-29

The jobs policy from 0003 only matched rows of the session's tenant (or platform mode), so a curator, who has no
tenant and doesn't run in platform mode, couldn't create a job at all (duplicate scans, discovery, imports). Jobs now
match like uploads and import rows (0008): tenant-less rows are visible to tenant-less sessions only. Inside the API,
JobService still limits non-auditors to their own jobs.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
IS_PLATFORM = "coalesce(current_setting('app.is_platform', true), 'off') = 'on'"


def _policy(match: str) -> None:
    op.execute("DROP POLICY tenant_isolation ON jobs")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON jobs
          USING ({match} OR {IS_PLATFORM})
          WITH CHECK ({match} OR {IS_PLATFORM})
        """
    )


def upgrade() -> None:
    # Replacing a policy needs an exclusive lock on jobs. Running jobs hold locks on it for minutes, so give up fast
    # instead of queueing (a queued exclusive lock would also block those jobs' progress updates); just retry.
    op.execute("SET LOCAL lock_timeout = '3s'")
    _policy(f"tenant_id IS NOT DISTINCT FROM {CURRENT_TENANT}")


def downgrade() -> None:
    _policy(f"tenant_id = {CURRENT_TENANT}")
