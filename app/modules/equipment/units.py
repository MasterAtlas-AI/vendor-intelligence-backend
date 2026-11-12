"""Reading engineering quantities out of free text ("1,800 m3/h", "20-1200 m³/h", "650 m", "150 barg", "5 MW").

Rule-based and local (no AI call, so no data leaves the server). Every value is converted to the canonical unit of
its quantity, so records from different sources compare. Ranges stay ranges: [min, max].
"""

import re

# quantity -> canonical unit, and every accepted unit -> factor to the canonical unit (temperature is special).
UNITS: dict[str, tuple[str, dict[str, float]]] = {
    "flow": (
        "m3/h",
        {
            "m3/h": 1,
            "m³/h": 1,
            "m3/hr": 1,
            "m³/hr": 1,
            "cmh": 1,
            "l/s": 3.6,
            "l/min": 0.06,
            "gpm": 0.2271247,
            "usgpm": 0.2271247,
            "bpd": 0.006624,
            "m3/d": 1 / 24,
            "m³/d": 1 / 24,
        },
    ),
    "head": ("m", {"m": 1, "mlc": 1, "ft": 0.3048}),
    "pressure": (
        "barg",
        {
            "barg": 1,
            "bar": 1,
            "bar(g)": 1,
            "psig": 0.0689476,
            "psi": 0.0689476,
            "mpa": 10,
            "kpa": 0.01,
            "kg/cm2": 0.980665,
        },
    ),
    "temperature": ("°C", {"°c": 1, "degc": 1, "deg c": 1, "oc": 1, "c": 1, "°f": 1, "degf": 1, "f": 1}),
    "power": (
        "kW",
        {"kw": 1, "kwe": 1, "kwm": 1, "kwb": 1, "mw": 1000, "hp": 0.7457, "bhp": 0.7457, "w": 0.001},
    ),
    "mass": (
        "t",
        {
            "t": 1,
            "mt": 1,  # metric tonnes, as engineering lists write it ("WEIGHT (MT)")
            "te": 1,
            "ton": 1,
            "tons": 1,
            "tonne": 1,
            "tonnes": 1,
            "kg": 0.001,
            "lb": 0.000453592,
            "lbs": 0.000453592,
        },
    ),
    "area": ("m2", {"m2": 1, "m²": 1, "sqm": 1, "ft2": 0.092903}),
    "voltage": ("V", {"v": 1, "kv": 1000}),
    "volume": ("m3", {"m3": 1, "m³": 1, "l": 0.001, "bbl": 0.158987}),
}
_FAHRENHEIT = {"°f", "degf", "f"}

_NUMBER = r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?"
_DASH = "-~" + chr(0x2013) + chr(0x2014)  # hyphen, tilde, en dash, em dash


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _unit_pattern(quantity: str) -> str:
    units = sorted(UNITS[quantity][1], key=len, reverse=True)
    return "|".join(re.escape(u) for u in units)


def parse_quantity(text: str, quantity: str) -> list[float] | None:
    """The first value or range of `quantity` in `text`, as [min, max] in the canonical unit; None if there is none.
    A unit is required (a bare number is ambiguous), except for a text that is only a number."""
    if quantity not in UNITS or not text:
        return None
    _, factors = UNITS[quantity]
    low = text.lower().replace("º", "°")
    bare = re.fullmatch(rf"\s*({_NUMBER})\s*", low)
    if bare:
        value = _num(bare.group(1))
        return [value, value]
    pattern = re.compile(
        rf"(?P<a>{_NUMBER})\s*(?:(?P<ua>{_unit_pattern(quantity)})\s*)?"
        rf"(?:[{_DASH}]|to)\s*(?P<b>{_NUMBER})\s*(?P<ub>{_unit_pattern(quantity)})(?![a-z0-9])"
        rf"|(?P<c>{_NUMBER})\s*(?P<uc>{_unit_pattern(quantity)})(?![a-z0-9])"
    )
    m = pattern.search(low)
    if not m:
        return None
    if m.group("c") is not None:
        values, unit = [_num(m.group("c"))] * 2, m.group("uc")
    else:
        values, unit = [_num(m.group("a")), _num(m.group("b"))], m.group("ub")
    if quantity == "temperature":
        if unit in _FAHRENHEIT:
            values = [(v - 32) * 5 / 9 for v in values]
    else:
        values = [v * factors[unit] for v in values]
    lo, hi = sorted(values)
    return [round(lo, 4), round(hi, 4)]


def canonical_unit(quantity: str) -> str:
    return UNITS[quantity][0] if quantity in UNITS else ""
