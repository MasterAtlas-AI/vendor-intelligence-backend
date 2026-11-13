"""File download headers."""

import re
from urllib.parse import quote


def attachment(filename: str, fallback: str = "download") -> dict[str, str]:
    """A Content-Disposition header for any file name: an ASCII name for old clients plus the UTF-8 name (RFC 6266).
    Header values must be Latin-1, so a name with curly quotes, Cyrillic or CJK would otherwise fail."""
    name = re.sub(r'[\r\n"\\]', "", filename or "").strip() or fallback
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name)
    return {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"}
