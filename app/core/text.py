"""Text clean-up shared by storage paths."""

from typing import Any

NUL = "\x00"


def strip_nul(value: Any) -> Any:
    """`value` with every NUL character removed from its strings (nested dicts, lists and tuples too).

    Postgres can't store NUL in text or jsonb ("unsupported Unicode escape sequence"); model output and scraped
    pages occasionally contain one, often where an accented letter was garbled ("R\\x00e9sidence")."""
    if isinstance(value, str):
        return value.replace(NUL, "") if NUL in value else value
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(strip_nul(v) for v in value)
    return value
