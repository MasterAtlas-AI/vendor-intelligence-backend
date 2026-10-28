"""Keyset (cursor) pagination over (created_at, id), newest first."""

import base64
import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.exceptions import ValidationFailed

ItemT = TypeVar("ItemT")
MAX_LIMIT = 200


class CursorPage(BaseModel, Generic[ItemT]):  # noqa: UP046 - pydantic generic model
    items: list[ItemT]
    next_cursor: str | None = None
    has_more: bool = False


def encode_cursor(created_at: datetime, id_: uuid.UUID) -> str:
    raw = json.dumps({"c": created_at.isoformat(), "i": str(id_)})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(data["c"]), uuid.UUID(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationFailed("Invalid cursor", errors=[{"field": "cursor", "message": "Invalid"}]) from exc


async def paginate(
    session: AsyncSession,
    stmt: Select[Any],
    *,
    created_at_col: InstrumentedAttribute[datetime],
    id_col: InstrumentedAttribute[uuid.UUID],
    limit: int,
    cursor: str | None,
) -> tuple[Sequence[Any], str | None]:
    """Returns (rows, next_cursor). `stmt` must select a single ORM entity."""
    limit = max(1, min(limit, MAX_LIMIT))
    if cursor:
        ts, last_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(created_at_col, id_col) < tuple_(ts, last_id))
    stmt = stmt.order_by(created_at_col.desc(), id_col.desc()).limit(limit + 1)
    rows = (await session.scalars(stmt)).all()
    if len(rows) <= limit:
        return rows, None
    rows = rows[:limit]
    last = rows[-1]
    return rows, encode_cursor(last.created_at, last.id)
