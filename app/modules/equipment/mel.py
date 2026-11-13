"""MEL import (SOW 3, 8, 27): read a project's Master Equipment List, classify each line against the taxonomy, match
its vendor and manufacturer to the knowledge base, read its ratings into structured attributes, and stage the rows
for review. Nothing becomes project history until a data administrator approves it.

Local and rule-based: no AI call, so the MEL never leaves the server (SOW 21).
"""

import re
import uuid
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.exceptions import NotFound, ValidationFailed
from app.domain.enums import VendorStatus
from app.domain.normalization import normalize_name
from app.modules.equipment.classify import classify
from app.modules.equipment.models import EquipmentAttribute, MelRow, ProjectEquipment
from app.modules.equipment.units import parse_quantity
from app.modules.imports.importer import Taxonomy
from app.modules.imports.models import Upload
from app.modules.imports.pdf_extract import PhraseIndex
from app.modules.imports.spreadsheet import (
    Sheet,
    Workbook,
    data_rows,
    detect_header_row,
    headers,
    read_spreadsheet,
)
from app.modules.vendors.models import Vendor, VendorAlias
from app.workers.runner import JobContext

# Fixed MEL columns; attribute columns come from the attribute definitions (their synonyms).
MEL_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tag": (
        "Tag",
        (
            "tag",
            "tag no",
            "tag number",
            "client equipment tag",
            "client tag",
            "equipment tag",
            "equipment tag no",
            "item tag",
            "tag nos",
            "equipment no",
        ),
    ),
    "name": (
        "Description",
        ("description", "equipment description", "equipment", "equipment name", "item description", "title"),
    ),
    "service": ("Service", ("service", "duty", "fluid", "process fluid", "service description", "medium")),
    "category": (
        "Equipment type",
        ("type", "equipment type", "category", "package type", "equipment category"),
    ),
    # "2 x 100%" in a configuration column is two units.
    "quantity": (
        "Quantity",
        (
            "qty",
            "quantity",
            "no off",
            "nos",
            "number",
            "no of units",
            "quantity and configuration",
            "configuration",
        ),
    ),
    "vendor": ("Vendor", ("vendor", "supplier", "package vendor", "vendor name", "contractor")),
    # "BRAND/MAKER" cells often read "packager / maker" ("EA POWER/ Cummins"): split when there's no vendor column.
    "manufacturer": (
        "Manufacturer",
        ("manufacturer", "make", "oem", "maker", "manufacturer name", "brand maker", "maker brand", "brand"),
    ),
    "model": ("Model", ("model", "model no", "model number", "series", "type model")),
    "standards": (
        "Codes / standards",
        (
            "code",
            "design code",
            "standard",
            "standards",
            "codes",
            "design standard",
            "equipment design code",
            "design code class",
        ),
    ),
    # Free-text ratings ("2000 kVA, 1600 kWe, 1800 rpm"): kept verbatim and read for attributes no column gave.
    "particulars": (
        "Particulars / ratings",
        (
            "particulars",
            "technical particulars",
            "main particulars",
            "specification",
            "specifications",
            "key data",
        ),
    ),
    "po_number": ("PO number", ("po", "po no", "po number", "purchase order", "order no")),
    # Quotation / bid evaluation registers (SOW 3): price, lead time and the technical bid evaluation result.
    "amount": (
        "Price (PO or quoted)",
        ("price", "po value", "po amount", "quoted price", "quote", "amount", "value"),
    ),
    "currency": ("Currency", ("currency", "curr", "ccy")),
    "lead_time": ("Lead time (weeks)", ("lead time", "lead time weeks", "delivery weeks", "delivery time")),
    "technical_status": (
        "TBE result",
        (
            "tbe",
            "tbe result",
            "tbe status",
            "technical status",
            "technical evaluation",
            "technically acceptable",
            "technical acceptance",
        ),
    ),
    "department": ("Discipline", ("discipline", "department")),
    "remarks": ("Remarks", ("remarks", "notes", "comments", "remark")),
}
DISCIPLINES = {
    "mechanical": "engineering",
    "process": "engineering",
    "electrical": "engineering",
    "instrument": "engineering",
    "instrumentation": "engineering",
    "piping": "engineering",
    "telecom": "engineering",
    "hvac": "engineering",
    "safety": "hse",
    "marine": "engineering",
}


# Raw MEL columns that carry prices: hidden from callers without commercial:read.
COMMERCIAL_HEADER = re.compile(
    r"price|amount|cost|quot|currency|\bccy\b|\bcurr\b|(po|order|contract|total|unit)\s*value|\b(usd|inr|eur|sgd|myr)\b",
    re.IGNORECASE,
)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


async def attribute_defs(s: AsyncSession) -> list[EquipmentAttribute]:
    """Shared defaults plus the session tenant's own (RLS returns both), in display order."""
    return list(await s.scalars(select(EquipmentAttribute).order_by(EquipmentAttribute.sort_order)))


def field_labels(defs: list[EquipmentAttribute]) -> dict[str, str]:
    out = {key: label for key, (label, _) in MEL_FIELDS.items()}
    out.update({f"attr:{d.key}": f"{d.label}{f' ({d.unit})' if d.unit else ''}" for d in defs})
    return out


# Working columns that only look like a field ("Weight Factor", "TOTAL DRY WT", "CAPACITY CHANGE FROM ...").
_DERIVED_HEADER = re.compile(r"\b(factor|total|totals|change|changes|ratio|percent|optimi[sz]e[ds]?)\b")


def suggest_mel_mapping(header_names: list[str], defs: list[EquipmentAttribute]) -> dict[str, int]:
    """Header -> field by synonym: exact first, then a header that starts with a synonym ("Capacity (m3/h)").
    A field takes the leftmost matching column: lists often carry look-up tables to the right of the equipment
    columns (a second "Description", of the P&ID)."""
    fields: list[tuple[str, set[str]]] = [
        (key, {_norm(s) for s in syns} - {""}) for key, (_, syns) in MEL_FIELDS.items()
    ]
    fields += [(f"attr:{d.key}", {_norm(s) for s in [d.label, d.key, *d.synonyms]} - {""}) for d in defs]
    names = [_norm(h) for h in header_names]
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for exact in (True, False):
        for field, syns in fields:
            if field in mapping:
                continue
            for i, name in enumerate(names):
                if i in used or not name or _DERIVED_HEADER.search(name):
                    continue
                if name in syns if exact else any(name.startswith(s + " ") for s in syns):
                    mapping[field] = i
                    used.add(i)
                    break
    return mapping


HEADER_SCAN = 30  # title blocks (document number, revision, client) sit above the header


def mel_header_row(sheet: Sheet, defs: list[EquipmentAttribute]) -> tuple[int, int]:
    """(1-based header row, mapped columns): the row among the first rows whose cells name the most MEL fields,
    so a title block ("MARINE EQUIPMENT LIST", "Doc No.") isn't taken for the header."""
    best, score = 0, 0
    for i, row in enumerate(sheet.rows[:HEADER_SCAN]):
        if sum(1 for v in row if v) < 3:
            continue
        hits = len(suggest_mel_mapping([" ".join(v.split()) for v in row], defs))
        if hits > score:
            best, score = i + 1, hits
    if score >= 3:
        return best, score
    return detect_header_row(sheet), 0


def _sheet_bias(name: str) -> int:
    """Workbooks keep working copies next to the list ("Weight Optimize", "working"): the sheet's name decides
    between sheets with the same columns."""
    n = _norm(name)
    if re.search(
        r"\b(work|working|optimi[sz]e|calc|calculation|cover|coversheet|contents|rev|revision)\b", n
    ):
        return -3
    return 3 if re.search(r"\bequipment\b|\bmel\b", n) else 0


def best_sheet(book: Workbook, defs: list[EquipmentAttribute]) -> Sheet:
    """The sheet that looks most like an equipment list (skips cover, revision and contents sheets)."""
    return max(book.sheets, key=lambda sh: (mel_header_row(sh, defs)[1] + _sheet_bias(sh.name), len(sh.rows)))


def sheet_info(sheet: Sheet, defs: list[EquipmentAttribute], limit: int) -> dict[str, Any]:
    row, score = mel_header_row(sheet, defs)
    names = headers(sheet, row)
    rows, _ = data_rows(sheet, row, limit)
    return {
        "name": sheet.name,
        "header_row": row,
        "headers": names,
        "data_rows": len(rows),
        "sample": [values for _, values in rows[:5]],
        "suggested_mapping": suggest_mel_mapping(names, defs),
        "_score": (score + _sheet_bias(sheet.name), len(rows)),
    }


def sheet_infos(book: Workbook, defs: list[EquipmentAttribute], limit: int) -> list[dict[str, Any]]:
    """Every sheet, the likeliest equipment list first (the review screen opens on the first)."""
    infos = sorted(
        (sheet_info(sh, defs, limit) for sh in book.sheets), key=lambda i: i["_score"], reverse=True
    )
    for info in infos:
        info.pop("_score")
    return infos


def read_mel(data: bytes, filename: str, max_rows: int) -> Workbook:
    return read_spreadsheet(data, filename, max_rows=max_rows)


class _Matcher:
    """Vendor and taxonomy matching for one import (each distinct name is looked up once)."""

    def __init__(self, s: AsyncSession, taxonomy: Taxonomy) -> None:
        self.s = s
        self.taxonomy = taxonomy
        self.index = PhraseIndex({"equipment": taxonomy.labels.get("equipment", [])})
        self.known = {code for kind, code in taxonomy.names if kind == "equipment"}
        self.vendors: dict[str, tuple[uuid.UUID | None, str]] = {}

    def category(self, *texts: str) -> str | None:
        """The category the type / description / service / section text names (the first field that names one
        wins; within it, the item named last: "Diesel Generator Day Tank" is a tank)."""
        return classify(self.index, self.known, texts)

    async def vendor(self, name: str) -> tuple[uuid.UUID | None, str]:
        """(vendor id, "exact" | "similar" | "") for a vendor name as written in the MEL."""
        key = normalize_name(name)
        if len(key) < 2:
            return None, ""
        if key in self.vendors:
            return self.vendors[key]
        active = Vendor.status == VendorStatus.ACTIVE
        vid = await self.s.scalar(
            select(Vendor.id)
            .outerjoin(VendorAlias, VendorAlias.vendor_id == Vendor.id)
            .where(active, or_(Vendor.normalized_name == key, VendorAlias.normalized_alias == key))
            .limit(1)
        )
        found: tuple[uuid.UUID | None, str] = (vid, "exact") if vid else (None, "")
        if vid is None:
            sim = func.similarity(Vendor.normalized_name, key)
            vid = await self.s.scalar(
                select(Vendor.id).where(active, sim > 0.55).order_by(sim.desc()).limit(1)
            )
            if vid:
                found = (vid, "similar")
        self.vendors[key] = found
        return found


def technical_status(text: str) -> str | None:
    """A TBE result as written ("TA", "Technically acceptable", "Acceptable with comments", "Not acceptable")."""
    t = _norm(text)
    if not t:
        return None
    if re.search(
        r"\b(not|un)\s*(technically\s+)?(acceptable|accepted|approved|ok|ta)\b|^no\b|reject|\bnta\b|fail", t
    ):
        return "rejected"
    plain = re.sub(r"\b(no|without) (comments?|deviations?|clarifications?|conditions?)\b", "", t)
    if re.search(r"condition|comment|clarif|deviation|subject to", plain):
        return "conditional"
    if re.search(r"accept|\bta\b|\bok\b|\bpass|approved|yes", t):
        return "accepted"
    return None


# Cells that mean "nothing here" in engineering lists (dash, soft hyphen, Excel errors).
_BLANK = {
    "-",
    "--",
    "---",
    chr(0x2013),
    chr(0x2014),
    chr(0xAD),
    "n/a",
    "na",
    "#n/a",
    "nil",
    "none",
    "#ref!",
    "#value!",
    # placeholders: to be advised / confirmed / determined, vendor to advise
    "tba",
    "tbc",
    "tbd",
    "vta",
    "xx",
    "see remarks",
    "see remark",
}


def _clean(value: str) -> str:
    v = value.strip()
    return "" if v.lower() in _BLANK else v


def _tag(value: str) -> str:
    """A tag as written, or "" for a placeholder: real tags carry a number ("P-101A", "20-PA-001")."""
    return value if re.search(r"\d", value) or " " not in value.strip() else ""


def _weeks(text: str) -> int | None:
    """A lead time in weeks: "16 weeks", "16-18 wks", "4 months", "90 days", or a bare number of weeks. A date
    ("2017-05-01") is not a lead time."""
    t = text.lower().strip()
    if not t or re.search(r"\d{4}-\d{1,2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}", t):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)(?:\s*(?:-|to)\s*\d+(?:\.\d+)?)?\s*([a-z]*)", t)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    if unit.startswith(("month", "mth", "mo")):
        n *= 4.345
    elif unit.startswith(("day", "d")) and not unit.startswith("deliv"):
        n /= 7
    elif unit and not unit.startswith(("w", "aro")):
        return None
    return min(round(n), 520)


def _quantity(text: str) -> int:
    """How many: "2", "2 x 50%", "1W + 1S" (working plus standby) = 2."""
    terms = re.findall(r"(\d+)\s*[a-z]?\s*(?:\+|$)", text.lower().strip())
    if "+" in text and len(terms) >= 2:
        n = sum(int(x) for x in terms)
    else:
        first = re.search(r"\d+", text)
        n = int(first.group()) if first else 1
    return max(1, min(n, 100_000))


def _cell(values: list[str], mapping: dict[str, int], field: str) -> str:
    i = mapping.get(field)
    return _clean(values[i]) if i is not None and i < len(values) else ""


def notes_start(values: list[str]) -> bool:
    """The "NOTES:" block under the list: everything below it is notes and reference lists, not equipment."""
    filled = [c for c in (_clean(v) for v in values) if c]
    return len(filled) <= 2 and any(re.fullmatch(r"(?i)notes?\s*:?", c) for c in filled)


def group_heading(values: list[str], mapping: dict[str, int]) -> str | None:
    """A section heading row ("POWER GENERATION SYSTEM", "(A25) MD: FLARE KNOCK OUT DRUMS & PUMPS"): no
    description, just a title and maybe a number. The rows below it belong to that system or package."""
    if _cell(values, mapping, "name") or _cell(values, mapping, "category"):
        return None
    # "P-101A | | | FLOWSERVE" is a line; "M40: PRODUCED WATER SYSTEM" in the tag column is still a heading.
    tag = _tag(_cell(values, mapping, "tag"))
    if re.search(r"\d", tag) and ":" not in tag and len(tag) <= 30 and len(tag.split()) <= 2:
        return None
    # Only the list's own columns count: look-up tables to the right and a "Part of M40 module" remark don't.
    last = max(mapping.values(), default=len(values) - 1)
    skip = {mapping.get("remarks")}
    filled = [c for i, v in enumerate(values[: last + 1]) if i not in skip and (c := _clean(v))]
    if not filled:
        return None
    makers = {mapping.get("vendor"), mapping.get("manufacturer")} - {None}
    if makers and all(i in makers for i, v in enumerate(values[: last + 1]) if i not in skip and _clean(v)):
        return None
    text = " ".join(max(filled, key=len).split())
    # A module summary ("M40: PRODUCED WATER SYSTEM / ..." with the module's size and weight) is a heading too.
    module = re.match(r"^\(?[A-Z]{1,3}\d*[A-Z]?\)?\s*[A-Z]{0,3}\d*[A-Z]?:\s+\S", text)
    if len(filled) > 2 and not module:
        return None
    if not re.search(r"[A-Za-z]{3}", text) or (" " not in text and not re.fullmatch(r"[A-Z&/]{4,}", text)):
        return None
    return re.sub(r"^\([A-Za-z0-9]+\)\s*", "", text)[:200]


_RANGE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s*$")
_NUMERIC = re.compile(r"[-\d.,\s]+(?:to[-\d.,\s]+)?")
_HEADER_UNIT = re.compile(r"\(([^)]+)\)|\[([^\]]+)\]")


def _read_number(raw: str, quantity: str, header: str) -> list[float] | None:
    """A cell of a numeric attribute column. "-29 / 120" is a range; a bare number takes the unit in the header
    ("DESIGN TEMP (oC)"); "FV / 7" under "(barg)" reads 7."""
    text = _RANGE.sub(r"\1 to \2", raw)
    value = parse_quantity(text, quantity)
    unit = _HEADER_UNIT.search(header)
    if unit and (value is None or _NUMERIC.fullmatch(text)):
        value = parse_quantity(f"{text} {unit.group(1) or unit.group(2)}", quantity) or value
    if value is None:  # "118.9 (See remarks)", "25 (Note 1)": the leading number, in the header's unit
        lead = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*(?:\(|\n|$)", text)
        if lead:
            return _read_number(lead.group(1), quantity, header)
    return value


# Quantities read from free text (particulars, capacity columns), only where the text clearly says so: a bare
# "28m" is as likely a crane radius as a pump head, "5L-20" is an engine model, not litres, and "25 MT" on a crane
# is its lift, not its weight.
_FREE_TEXT_GUARD: dict[str, re.Pattern[str]] = {
    "flow": re.compile(r"."),
    "pressure": re.compile(r"."),
    "power": re.compile(r"."),
    "voltage": re.compile(r"."),
    "head": re.compile(r"\bhead\b", re.I),
    "volume": re.compile(r"\d\s*(m3|m³|bbl)(?!\s*/)", re.I),
}


def _dimensions(names: list[str], values: list[str]) -> str:
    """L / W / H in separate columns ("L or T/T (m)", "W or I.D (m)", "H or T/T (m)") as one text."""
    parts: list[str] = []
    unit = ""
    for letter in ("l", "w", "h"):
        for i, name in enumerate(names):
            n = _norm(name)
            if n == letter or n.startswith(f"{letter} or ") or n == f"{letter} m":
                v = _clean(values[i]) if i < len(values) else ""
                if v:
                    parts.append(f"{letter.upper()} {v}")
                    found = _HEADER_UNIT.search(name)
                    if found and not unit:
                        unit = found.group(1) or found.group(2)
                break
    return f"{' x '.join(parts)}{f' {unit}' if unit else ''}" if len(parts) >= 2 else ""


# Columns that each hold an "X" for the equipment's state (marine lists: SCRAPPED | REFURBISHED | NEW).
CONDITIONS = ("new", "refurbished", "scrapped", "reused", "existing")


async def parse_row(
    m: _Matcher,
    defs: list[EquipmentAttribute],
    names: list[str],
    values: list[str],
    mapping: dict[str, int],
    group: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    def cell(field: str) -> str:
        return _cell(values, mapping, field)

    issues: list[str] = []
    name = cell("name") or cell("category")
    if not name:
        issues.append("No description")
    category = m.category(cell("category"), cell("name"), cell("service"), group or "")
    if category is None:
        issues.append("Equipment type not recognised: pick a category")
    parsed: dict[str, Any] = {
        "tag": _tag(cell("tag"))[:120],
        "name": name[:300],
        "service": cell("service")[:300],
        "category_code": category,
        "category_name": m.taxonomy.names.get(("equipment", category)) if category else None,
        "model": cell("model")[:200],
        "po_number": cell("po_number")[:80],
        "standards": [s.strip() for s in re.split(r"[;,/\n]", cell("standards")) if s.strip()][:20],
    }
    parsed["quantity"] = _quantity(cell("quantity"))
    discipline = _norm(cell("department"))
    parsed["department"] = next((d for k, d in DISCIPLINES.items() if k in discipline), None)
    written_as = {"vendor": cell("vendor"), "manufacturer": cell("manufacturer")}
    if not written_as["vendor"] and "/" in written_as["manufacturer"]:
        packager, _, maker = written_as["manufacturer"].partition("/")
        if packager.strip() and maker.strip():  # "EA POWER/ Cummins": package vendor / equipment maker
            written_as = {"vendor": packager.strip(), "manufacturer": maker.strip()}
    for role in ("vendor", "manufacturer"):
        written = written_as[role][:300]
        vid, how = await m.vendor(written) if written else (None, "")
        parsed[f"{role}_name"], parsed[f"{role}_id"], parsed[f"{role}_match"] = written, vid and str(vid), how
        if written and not vid:
            issues.append(f"{role.capitalize()} '{written[:60]}' isn't in the knowledge base (kept as text)")
    attributes: dict[str, Any] = {}
    unread: list[str] = []  # numeric columns holding words ("Engine Driven", "Atm"): kept as text
    for d in defs:
        raw = cell(f"attr:{d.key}")
        if not raw:
            continue
        if d.data_type == "text":
            attributes[d.key] = raw[:300]
            continue
        # The header may carry the unit ("Capacity (m3/h)"): try the cell alone, then cell + header unit.
        header = names[mapping[f"attr:{d.key}"]] if mapping.get(f"attr:{d.key}") is not None else ""
        value = _read_number(raw, d.quantity, header)
        if value is None:
            unread.append(f"{header or d.label}: {' '.join(raw.split())}")
            if re.search(r"\d", raw):  # a figure we couldn't read is worth a look; plain words aren't
                issues.append(f"Couldn't read {d.label.lower()} '{raw[:40]}'")
            continue
        attributes[d.key] = value[0] if value[0] == value[1] else value
    # Ratings written as text: the particulars column, and capacity columns no attribute claimed.
    used = set(mapping.values())
    free = [cell("particulars")] + [
        _clean(v)
        for i, v in enumerate(values)
        if i not in used and i < len(names) and "capacity" in _norm(names[i])
    ]
    for text in (t for t in free if t and not _NUMERIC.fullmatch(t)):
        for d in defs:
            guard = _FREE_TEXT_GUARD.get(d.quantity)
            if d.key in attributes or d.data_type == "text" or guard is None or not guard.search(text):
                continue
            value = parse_quantity(text, d.quantity)
            if value is not None:
                attributes[d.key] = value[0] if value[0] == value[1] else value
    if "dimensions" not in attributes and any(d.key == "dimensions" for d in defs):
        dims = _dimensions(names, values)
        if dims:
            attributes["dimensions"] = dims[:300]
    parsed["attributes"] = attributes
    amount = re.sub(r"[^\d.]", "", cell("amount").replace(",", ""))
    parsed["amount"] = amount if re.fullmatch(r"\d+(\.\d+)?", amount or "") else None
    if cell("amount") and parsed["amount"] is None:
        issues.append(f"Couldn't read price '{cell('amount')[:40]}'")
    currency = re.search(r"\b[A-Za-z]{3}\b", cell("currency") or cell("amount"))
    parsed["currency"] = currency.group().upper() if currency else None
    parsed["lead_time_weeks"] = _weeks(cell("lead_time"))
    if cell("lead_time") and parsed["lead_time_weeks"] is None:
        issues.append(f"Couldn't read lead time '{cell('lead_time')[:40]}'")
    parsed["technical_status"] = technical_status(cell("technical_status"))
    condition = [
        names[i]
        for i, v in enumerate(values)
        if i < len(names) and _norm(names[i]) in CONDITIONS and _clean(v)
    ]
    parsed["system"] = group or ""
    parsed["condition"] = condition[0].capitalize() if len(condition) == 1 else ""
    extra = [
        f"{names[i]}: {_clean(v)}"
        for i, v in enumerate(values)
        if i not in used and _clean(v) and i < len(names) and _norm(names[i]) not in CONDITIONS
    ]
    lead = [
        f"{label}: {text}"
        for label, text in (
            ("System / package", parsed["system"]),
            ("Condition", parsed["condition"]),
            ("Particulars", cell("particulars")),
            ("Remarks", cell("remarks")),
        )
        if text
    ]
    parsed["specs_text"] = "\n".join(lead + unread + extra)[:20_000]
    return parsed, issues


def line_key(tag: str | None, name: str | None, system: str | None = None) -> str:
    """What makes two MEL lines the same equipment: the tag ("P-101 A" = "p-101a"), or for an untagged line its
    description within its system or package ("lube oil pump" of the gas compressor package)."""
    t = re.sub(r"\s+", "", (tag or "").upper())
    if t:
        return f"tag:{t}"
    within = " ".join((system or "").lower().split())
    return f"name:{within}|" + " ".join((name or "").lower().split())


_SYSTEM_LINE = re.compile(r"^System / package: (.+)$", re.MULTILINE)


async def mel_import_handler(ctx: JobContext) -> dict[str, Any]:
    """Parses an uploaded MEL into reviewable rows (replacing that upload's pending rows)."""
    inp = ctx.input
    upload_id, project_id = uuid.UUID(inp["upload_id"]), uuid.UUID(inp["project_id"])
    await ctx.progress("reading", 5)
    async with ctx.session() as s:
        upload = await s.scalar(select(Upload).where(Upload.id == upload_id).options(undefer(Upload.data)))
        if upload is None:
            raise NotFound("The uploaded MEL no longer exists")
        book = read_mel(upload.data, upload.filename, ctx.settings.import_max_rows + 50)
        defs = await attribute_defs(s)
        sheet = book.sheet(inp["sheet"]) if inp.get("sheet") else best_sheet(book, defs)
        header_row = inp.get("header_row") or mel_header_row(sheet, defs)[0]
        names = headers(sheet, header_row)
        mapping = inp.get("mapping") or suggest_mel_mapping(names, defs)
        if "name" not in mapping and "category" not in mapping:
            raise ValidationFailed("Map a Description or Equipment type column")
        rows, truncated = data_rows(sheet, header_row, ctx.settings.import_max_rows)
        matcher = _Matcher(s, await Taxonomy.load(s))
        await s.execute(delete(MelRow).where(MelRow.upload_id == upload_id, MelRow.status == "pending"))
        # SOW 8.9: lines already recorded for this project (a re-import or a newer MEL revision).
        # Untagged lines often repeat ("Lube oil pump" in each compressor train): every recorded line is matched
        # once, in order, so N identical lines update N lines rather than the first one N times.
        recorded: dict[str, list[tuple[uuid.UUID, str]]] = {}
        for eid, etag, ename, especs in await s.execute(
            select(
                ProjectEquipment.id, ProjectEquipment.tag, ProjectEquipment.name, ProjectEquipment.specs_text
            )
            .where(ProjectEquipment.project_id == project_id, ProjectEquipment.verification != "archived")
            .order_by(ProjectEquipment.created_at, ProjectEquipment.id)
        ):
            system = _SYSTEM_LINE.search(especs or "")
            key = line_key(_tag(etag or ""), ename, system.group(1) if system else None)
            recorded.setdefault(key, []).append((eid, etag or ename))
        seen: dict[str, int] = {}  # key -> row number, within this file
        counts = {
            "rows": 0,
            "with_issues": 0,
            "categorised": 0,
            "vendors_matched": 0,
            "sections": 0,
            "skipped": 0,
            "duplicates": 0,
        }
        group: str | None = None
        for i, (row_number, values) in enumerate(rows):
            if not any(_clean(v) for v in values):
                continue
            if notes_start(values):
                break
            heading = group_heading(values, mapping)
            if heading:  # a section title, not equipment: it labels the rows below
                group = heading
                counts["sections"] += 1
                continue
            if not any(_cell(values, mapping, f) for f in ("name", "category", "tag")):
                counts["skipped"] += 1  # deleted lines ("A4 | - | - | ... Deleted"), "NOTE:" and the like
                continue
            parsed, issues = await parse_row(matcher, defs, names, values, mapping, group)
            key = line_key(parsed["tag"], parsed["name"], parsed.get("system"))
            if recorded.get(key):
                eid, label = recorded[key].pop(0)
                parsed["duplicate_of"] = str(eid)
                issues.append(f"Already recorded on this project ({label[:60]}): approving updates that line")
                counts["duplicates"] += 1
            elif key in seen:
                issues.append(
                    f"Same {'tag' if parsed['tag'] else 'description'} as row {seen[key]} of this file"
                )
                counts["duplicates"] += 1
            seen.setdefault(key, row_number)
            s.add(
                MelRow(
                    tenant_id=ctx.tenant_id,
                    upload_id=upload_id,
                    project_id=project_id,
                    row_number=row_number,
                    raw={names[j] or f"Column {j + 1}": v for j, v in enumerate(values) if v.strip()},
                    parsed=parsed | {"source_ref": f"{upload.filename}, {sheet.name}, row {row_number}"},
                    issues=[x[:200] for x in issues],
                )
            )
            counts["rows"] += 1
            counts["with_issues"] += bool(issues)
            counts["categorised"] += parsed["category_code"] is not None
            counts["vendors_matched"] += bool(parsed["vendor_id"])
            if i % 200 == 199:
                await s.flush()
                await ctx.progress("matching", 10 + int(80 * i / max(len(rows), 1)))
        await s.commit()
    return counts | {"truncated": truncated, "upload_id": str(upload_id), "project_id": str(project_id)}
