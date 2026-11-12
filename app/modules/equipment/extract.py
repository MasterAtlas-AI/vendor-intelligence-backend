"""Structured attributes from a catalogue product's free-text specs (SOW 5.2, review change 3).

`{"Max head": "650 m", "Capacities": "20-1,200 m3/h"}` -> `{"head": [650, 650], "capacity": [20, 1200]}`, matched by
the shared attribute definitions' labels and synonyms and converted to their units. Rule-based and local; shown as
"extracted from specs, pending verification", never as verified vendor data.
"""

import re
from typing import Any

from app.modules.equipment.seed import DEFAULTS
from app.modules.equipment.units import parse_quantity

_NUMERIC = [
    (key, quantity, (label, key, *synonyms)) for key, label, quantity, synonyms in DEFAULTS if quantity
]


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def extract_spec_attributes(specs: dict[str, Any]) -> dict[str, list[float]]:
    """Each spec whose label names a numeric attribute and whose value carries a unit of its quantity."""
    out: dict[str, list[float]] = {}
    for label, value in (specs or {}).items():
        name = _norm(str(label))
        if not name or value is None:
            continue
        for key, quantity, names in _NUMERIC:
            if key in out:
                continue
            if any(
                name == _norm(n) or name.startswith(_norm(n) + " ") or name.endswith(" " + _norm(n))
                for n in names
            ):
                parsed = parse_quantity(str(value), quantity)
                if parsed and not re.fullmatch(
                    r"[\d.,\s-]+", str(value)
                ):  # a unit is required in catalogue text
                    out[key] = parsed
                    break
    return out
