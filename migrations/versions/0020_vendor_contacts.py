"""Company contact points for vendors (role mailboxes and phone lines, no named people)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-01

vendor_contacts is part of the shared knowledge base like vendor_locations (no RLS; written only through approved
proposals). Postgres can't drop enum values, so the downgrade leaves 'add_contact' in place (harmless).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE proposal_item_action ADD VALUE IF NOT EXISTS 'add_contact'")
    op.execute(
        """
        CREATE TABLE vendor_contacts (
            vendor_id UUID NOT NULL,
            contact_type VARCHAR(20) NOT NULL,
            label VARCHAR(120) NOT NULL,
            email VARCHAR(254),
            phone VARCHAR(40),
            country CHAR(2),
            status fact_status NOT NULL,
            id UUID NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            created_by UUID,
            updated_by UUID,
            CONSTRAINT pk_vendor_contacts PRIMARY KEY (id),
            CONSTRAINT fk_vendor_contacts_vendor_id_vendors FOREIGN KEY (vendor_id) REFERENCES vendors (id)
                ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX ix_vendor_contacts_vendor_id ON vendor_contacts (vendor_id)")


def downgrade() -> None:
    op.execute("DROP TABLE vendor_contacts")
