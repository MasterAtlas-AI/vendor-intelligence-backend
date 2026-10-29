from datetime import datetime
from typing import Any

from sqlalchemy import CHAR, DateTime, Integer, LargeBinary, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base
from app.db.mixins import Entity


class WebPage(Entity, Base):
    """The latest fetched copy of a public web page or PDF, shared by all organizations (public content, no tenant).

    `sha256` is of the extracted text: when a re-fetch yields the same hash, `changed_at` stays put and pipelines can
    skip re-extracting facts from it.
    """

    __tablename__ = "web_pages"

    url: Mapped[str] = mapped_column(Text, unique=True)  # normalised (see crawler.urls.normalize_url)
    final_url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(255), index=True)  # registrable domain
    status: Mapped[int] = mapped_column(SmallInteger)
    content_type: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(String(500), default="")
    language: Mapped[str] = mapped_column(String(12), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    links: Mapped[list[Any]] = mapped_column(JSONB, default=list)  # [[url, anchor], ...] same-site links
    page_count: Mapped[int | None] = mapped_column(Integer)  # PDFs
    sha256: Mapped[str | None] = mapped_column(CHAR(64))
    etag: Mapped[str | None] = mapped_column(String(200))
    last_modified: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(String(300))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # PDF bytes, kept so documents can be re-read without fetching again. Deferred: never loaded unless asked for.
    raw: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))
