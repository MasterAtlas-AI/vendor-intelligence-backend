"""Uploaded files and the per-row outcome of an import (the import report).

Both are tenant-private (RLS). `tenant_id` is NULL for uploads by platform curators who aren't acting as a
tenant; those rows are visible to tenant-less users only.
"""

import uuid
from typing import Any

from sqlalchemy import CHAR, BigInteger, ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base, pg_enum
from app.db.mixins import Entity
from app.domain.enums import ImportRowOutcome, UploadKind


class Upload(Entity, Base):
    __tablename__ = "uploads"
    __table_args__ = (Index("ix_uploads_tenant_id_created_at", "tenant_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    kind: Mapped[UploadKind] = mapped_column(pg_enum(UploadKind, "upload_kind"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(CHAR(64))
    # Sheet names, header rows and row counts found when the file was uploaded.
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # The file itself. Deferred so listing uploads never pulls megabytes over the wire.
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary))


class ImportRow(Entity, Base):
    __tablename__ = "import_rows"
    __table_args__ = (
        Index("ix_import_rows_job_id_row_number", "job_id", "row_number"),
        # Rows still waiting for their proposal to be approved before the vendor joins the tenant's list.
        Index(
            "ix_import_rows_pending_link",
            "proposal_id",
            postgresql_where="vendor_id IS NULL AND proposal_id IS NOT NULL",
        ),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    upload_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("uploads.id", ondelete="SET NULL"))
    row_number: Mapped[int] = mapped_column(Integer)  # as shown in the spreadsheet (1-based, header included)
    outcome: Mapped[ImportRowOutcome] = mapped_column(pg_enum(ImportRowOutcome, "import_row_outcome"))
    vendor_name: Mapped[str] = mapped_column(String(300), default="")
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_proposals.id", ondelete="SET NULL")
    )
    # The tenant's own fields from the row (AVL status, code, tags, notes), applied once the vendor exists.
    overlay: Mapped[dict[str, Any]] = mapped_column(default=dict)
    messages: Mapped[list[str]] = mapped_column(JSONB, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(
        default=dict
    )  # AI discovery: website, highlights, sources
