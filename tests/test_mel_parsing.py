"""MEL reading on real-world layouts (no database): title blocks above the header, cover sheets and working copies
next to the list, section headings, look-up columns to the right, "packager / maker" cells and engineering
notation ("FV / 7", "-29 / 120", "2 x 100%", "VTA")."""

import io
import uuid
from typing import Any

from openpyxl import Workbook

from app.modules.equipment.mel import (
    best_sheet,
    group_heading,
    mel_header_row,
    notes_start,
    parse_row,
    sheet_infos,
    suggest_mel_mapping,
)
from app.modules.equipment.seed import DEFAULTS
from app.modules.equipment.units import parse_quantity
from app.modules.imports.spreadsheet import data_rows, headers, read_spreadsheet


class _Def:
    def __init__(self, key: str, label: str, quantity: str, synonyms: tuple[str, ...]) -> None:
        self.key, self.label, self.quantity, self.synonyms = key, label, quantity, list(synonyms)
        self.data_type = "number" if quantity else "text"
        self.unit = ""


DEFS: Any = [_Def(*d) for d in DEFAULTS]


class _Matcher:
    """No taxonomy or vendor database: categories and vendors stay unmatched."""

    class taxonomy:  # noqa: N801
        names: dict[tuple[str, str], str] = {}

    def category(self, *texts: str) -> str | None:
        return None

    async def vendor(self, name: str) -> tuple[uuid.UUID | None, str]:
        return None, ""


def _book(sheets: dict[str, list[list[object]]]) -> bytes:
    wb = Workbook()
    first = True
    for title, rows in sheets.items():
        ws = wb.active if first else wb.create_sheet()
        assert ws is not None
        ws.title = title
        first = False
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


MARINE_HEADER = [
    "REV.", "No.", "TAG NUMBER", "RFQ NO.", "EQUIPMENT DESCRIPTION", "CONFIGURATION", "SYSTEM\nCODE", "SCRAPPED",
    "REFURBISHED", "NEW", "PARTICULARS", "ELECTRICAL ABSORBED POWER", "BRAND/MAKER", "DIMENSION (mm)\nL x W x H",
    "WEIGHT (MT)\n(DRY)", "WEIGHT(MT)\n(OPER)", "LOCATION", "REMARKS", None, "Document Number", "Description",
]  # fmt: skip


def test_marine_list_title_block_sections_and_packager_maker() -> None:
    rows: list[list[object]] = [
        [None, None, None, None, "MARINE EQUIPMENT LIST", *[None] * 11, "Doc No.:", "20171-SPOG-60000-MA-LS-0001"],
        [*[None] * 16, "Rev. No.:", "P3"],
        [],
        MARINE_HEADER,
        [1, "POWER GENERATION SYSTEM", *[None] * 17, "20171-SPOG--MA-DW-", "#N/A"],
        [None, 1, "A-F11010", "020", "Emergency Diesel Generator Package", "1 x 100%", 11, None, None, "X",
         "2000kVa, 1600 kWe, 1800rpm", "-", "EA POWER/ Cummins", "7000 x 3000 x 3700", 20, 20.3,
         "Emergency Generator Room", None, None, "20171-SPOG-65000-MA-DW-0002", "P&ID EXHAUST GAS SYSTEM"],
        [2, "BALLAST SYSTEM"],
        [None, 2, "P-F21010 A/B", "-", "Ballast Pump", "2 x 100%", 21, None, "X", None, "1200 m3/hr @ 30 mlc",
         "160 kW", "Shinko", "-", "TBA", None, "Pump Room", "Steam Turbine Driven"],
        [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
         "Part of package"],
        ["NOTES:"],
        [None, None, "X-0001", None, "Not equipment: below the notes"],
    ]  # fmt: skip
    data = _book({"Cover Sheet": [["SHAPOORJI"], ["Marine Equipment List"]], "Equipment List": rows})
    book = read_spreadsheet(data, "mel.xlsx", max_rows=100)

    assert next(s["name"] for s in sheet_infos(book, DEFS, 100)) == "Equipment List"
    sheet = best_sheet(book, DEFS)
    assert sheet.name == "Equipment List"
    header_row, mapped = mel_header_row(sheet, DEFS)
    assert header_row == 4 and mapped >= 8  # not the title block in row 1
    names = headers(sheet, header_row)
    assert names[6] == "SYSTEM CODE"  # line breaks inside header cells are flattened
    mapping = suggest_mel_mapping(names, DEFS)
    assert (
        mapping["tag"] == 2 and mapping["name"] == 4
    )  # not the P&ID "Description" look-up column on the right
    assert mapping["quantity"] == 5 and mapping["manufacturer"] == 12 and mapping["particulars"] == 10
    assert mapping["attr:power"] == 11 and mapping["attr:weight"] == 14 and mapping["attr:dimensions"] == 13


async def test_marine_rows_parse() -> None:
    names = [" ".join(str(h or "").split()) for h in MARINE_HEADER]
    mapping = suggest_mel_mapping(names, DEFS)
    heading = [str(v) if v is not None else "" for v in [1, "POWER GENERATION SYSTEM", *[None] * 17]]
    heading += ["20171-SPOG--MA-DW-", "#N/A"]
    assert group_heading(heading, mapping) == "POWER GENERATION SYSTEM"  # look-up columns don't count
    assert group_heading(["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "Part of package"],
                         mapping) is None  # fmt: skip
    assert notes_start(["NOTES:"]) and notes_start(["", "NOTE :"]) and not notes_start(["Notes on pump"])

    edg = ["", "1", "A-F11010", "020", "Emergency Diesel Generator Package", "1 x 100%", "11", "", "", "X",
           "2000kVa, 1600 kWe, 1800rpm", "-", "EA POWER/ Cummins", "7000 x 3000 x 3700", "20", "20.3",
           "Emergency Generator Room", "", "", "20171-SPOG-65000-MA-DW-0002", "P&ID EXHAUST GAS SYSTEM"]  # fmt: skip
    p, issues = await parse_row(_Matcher(), DEFS, names, edg, mapping, "POWER GENERATION SYSTEM")  # type: ignore[arg-type]
    assert (p["tag"], p["name"], p["quantity"]) == ("A-F11010", "Emergency Diesel Generator Package", 1)
    assert (p["vendor_name"], p["manufacturer_name"]) == ("EA POWER", "Cummins")  # packager / maker
    assert p["system"] == "POWER GENERATION SYSTEM" and p["condition"] == "New"
    assert p["attributes"]["weight"] == 20.0  # "(MT)" is metric tonnes
    assert p["attributes"]["power"] == 1600.0  # from the particulars: the power column says "-"
    assert p["attributes"]["dimensions"] == "7000 x 3000 x 3700"
    assert "System / package: POWER GENERATION SYSTEM" in p["specs_text"]
    assert "Particulars: 2000kVa, 1600 kWe, 1800rpm" in p["specs_text"]
    assert "Document Number: 20171-SPOG-65000-MA-DW-0002" in p["specs_text"]
    assert not [i for i in issues if i.startswith("Couldn't")]

    pump = ["", "2", "P-F21010 A/B", "-", "Ballast Pump", "2 x 100%", "21", "", "X", "", "1200 m3/hr @ 30 mlc",
            "160 kW", "Shinko", "-", "TBA", "", "Pump Room", "Steam Turbine Driven"]  # fmt: skip
    p, issues = await parse_row(_Matcher(), DEFS, names, pump, mapping, "BALLAST SYSTEM")  # type: ignore[arg-type]
    assert p["quantity"] == 2 and p["condition"] == "Refurbished"
    assert (p["vendor_name"], p["manufacturer_name"]) == ("", "Shinko")
    assert p["attributes"] == {
        "power": 160.0,
        "capacity": 1200.0,
    }  # head needs the word "head"; "TBA" is blank
    assert "Remarks: Steam Turbine Driven" in p["specs_text"]
    assert not [i for i in issues if i.startswith("Couldn't")]


TOPSIDES_HEADER = [
    "REV No.", "OLD 'EQUIPMENT / TAG No.", "CLIENT EQUIPMENT  TAG", "DESCRIPTION", "VENDOR", "EQUIPMENT TYPE",
    "MODULE", "EQUIPMENT DESIGN CODE/CLASS", "ORIENTATION", "MATERIAL OF CONSTRUCTION", "CONFIGURATION",
    "OPERATING PRESS (barg)", "DESIGN PRESS (barg)", "DESIGN TEMP (oC)", "DESIGN FLOW m3/hr",
    "PUMP / COMPRESSOR / TANK CAPACITY", "ABSORBED POWER PER UNIT (kW)", "L or T/T (m)", "W or I.D (m)",
    "H or T/T (m)", "Weight Factor", "DRY WT       in MT", "TOTAL DRY WT in mT", "REMARKS",
]  # fmt: skip


async def test_topsides_list_working_copy_and_engineering_notation() -> None:
    title: list[list[object]] = [
        [None, None, None, None, None, None, "TOPSIDES EQUIPMENT LIST", None, None, "COMPANY", "ONGC"],
        [None] * 9 + ["PROJECT", "98/2 FPSO"],
        [],
        [],
    ]
    rows = [
        *title,
        TOPSIDES_HEADER,
        [None, "(A25) MD: FLARE KNOCK OUT DRUMS & PUMPS / CLOSED DRAIN VESSEL & PUMPS"],
        [None, "M40: PRODUCED WATER SYSTEM", "M40: PRODUCED WATER SYSTEM", None, "VME PROCESS", None, None, None,
         None, None, None, None, None, None, None, None, None, 30, 21, 9, None, 1181],
        [None, "20-PR-1810 A/B", "P-S68115 A/B", "LP Flare Pumps", "NETZCH", "Progressive Cavity Pump", "MAIN DECK",
         "API 676", "-", "DSS 22Cr", "2 x 100%", "0.1 / 0.5", "FV / 12", "-5 / 150", "-", "22 m3/hr", 4, 2.964,
         1.125, 2.252, "DSS 22Cr", "1.035 (See remarks)", 2.07, None],
        ["A4", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-",
         "-", "-", "Deleted"],
        [None, None, "V-S24515", "Desanding Hydrocyclone", "VESSELTECH", "Pressure Vessel", "MAIN DECK", "ASME VIII",
         "Vertical", "SDSS", "1 x 100%", "8.0 / 30.0", "VTA", "0 / 150", "350 Nm3/hr"],
    ]  # fmt: skip
    data = _book({"Weight Optimize": rows, "EQUIPMENT LIST": rows, "Cover Sheet - 2": title})
    book = read_spreadsheet(data, "tel.xlsx", max_rows=100)
    sheet = best_sheet(book, DEFS)
    assert sheet.name == "EQUIPMENT LIST"  # same columns as the working copy: the name decides
    header_row, _ = mel_header_row(sheet, DEFS)
    assert header_row == 5
    names = headers(sheet, header_row)
    mapping = suggest_mel_mapping(names, DEFS)
    assert mapping["tag"] == 2  # the client tag, not the old one
    assert (
        mapping["standards"] == 7 and mapping["attr:design_pressure"] == 12 and mapping["attr:weight"] == 21
    )
    assert 20 not in mapping.values() and 22 not in mapping.values()  # "Weight Factor", "TOTAL DRY WT"

    rows_read, _ = data_rows(sheet, header_row, 100)
    cells = [values for _, values in rows_read]
    assert (
        group_heading(cells[0], mapping) == "MD: FLARE KNOCK OUT DRUMS & PUMPS / CLOSED DRAIN VESSEL & PUMPS"
    )
    assert group_heading(cells[1], mapping) == "M40: PRODUCED WATER SYSTEM"  # a module summary line
    assert (
        group_heading(cells[3], mapping) is None
    )  # the deleted line has no description: the import skips it

    p, issues = await parse_row(_Matcher(), DEFS, names, cells[2], mapping, "FLARE KNOCK OUT DRUMS")  # type: ignore[arg-type]
    assert (p["tag"], p["vendor_name"], p["manufacturer_name"], p["quantity"]) == (
        "P-S68115 A/B",
        "NETZCH",
        "",
        2,
    )
    assert p["standards"] == ["API 676"]
    a = p["attributes"]
    assert a["design_pressure"] == 12.0  # "FV / 12": full vacuum to 12 barg
    assert a["design_temperature"] == [-5.0, 150.0]
    assert a["capacity"] == 22.0  # the design-flow column is "-": read from the capacity column
    assert a["power"] == 4.0 and a["weight"] == 1.035 and a["material"] == "DSS 22Cr"
    assert a["dimensions"] == "L 2.964 x W 1.125 x H 2.252 m"
    assert not issues or all(not i.startswith("Couldn't") for i in issues)

    p, issues = await parse_row(_Matcher(), DEFS, names, cells[4], mapping, None)  # type: ignore[arg-type]
    assert "design_pressure" not in p["attributes"]  # "VTA": vendor to advise
    assert p["attributes"]["design_temperature"] == [0.0, 150.0]
    # Normal m3 of gas is not a liquid flow: flagged for the reviewer, and kept with the row's specs.
    assert "capacity" not in p["attributes"]
    assert [i for i in issues if i.startswith("Couldn't")] == ["Couldn't read capacity / flow '350 Nm3/hr'"]
    assert "DESIGN FLOW m3/hr: 350 Nm3/hr" in p["specs_text"]


def test_units_used_by_engineering_lists() -> None:
    assert parse_quantity("20 MT", "mass") == [20.0, 20.0]
    assert parse_quantity("120 oC", "temperature") == [120.0, 120.0]
    assert parse_quantity("1600 kWe", "power") == [1600.0, 1600.0]


def test_review_fixes_tbe_lead_time_quantity_tags_and_units() -> None:
    from app.modules.equipment.mel import _quantity, _read_number, _tag, _weeks, line_key, technical_status

    assert [technical_status(x) for x in (
        "Not technically acceptable", "NOT TA", "Not OK", "Acceptable, no comments", "Acceptable with comments", "TA",
    )] == ["rejected", "rejected", "rejected", "accepted", "conditional", "accepted"]  # fmt: skip
    assert [_weeks(x) for x in ("16-18 wks", "6 months", "90 days", "2017-05-01", "16 ARO")] == [
        16,
        26,
        13,
        None,
        16,
    ]
    assert [_quantity(x) for x in ("2 x 50%", "1W + 1S", "")] == [2, 2, 1]
    assert _tag("From Project Tag Numbering Procedure") == "" and _tag("P-101A") == "P-101A"
    assert _read_number("212", "temperature", "DESIGN TEMP (oF)") == [100.0, 100.0]
    # A tagged line whose only other text is the vendor is equipment, not a section heading.
    assert (
        group_heading(["P-101A", "", "", "FLOWSERVE"], {"tag": 0, "name": 1, "service": 2, "vendor": 3})
        is None
    )
    # Untagged lines are told apart by their system or package.
    assert line_key("", "Lube oil pump", "Train A") != line_key("", "Lube oil pump", "Train B")
