"""Spreadsheet reading, header detection, column mapping and row parsing (no database)."""

import io

import pytest
from openpyxl import Workbook

from app.core.exceptions import ValidationFailed
from app.domain.enums import AvlStatus
from app.modules.imports.importer import ImportOptions, Taxonomy, _term_key, parse_avl, parse_row, split_list
from app.modules.imports.service import _csv_safe
from app.modules.imports.spreadsheet import (
    data_rows,
    detect_header_row,
    headers,
    read_spreadsheet,
    suggest_mapping,
)


def xlsx(rows: list[list[object]], title: str = "Vendors") -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = title
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_reads_xlsx_and_detects_header_below_a_title() -> None:
    data = xlsx(
        [
            ["Approved vendor list 2026"],
            [],
            ["Supplier Name", "Web Site", "Country", "Vendor Code", "AVL"],
            ["Acme Pumps Ltd", "www.acme-pumps.com", "United Kingdom", 1042.0, "Yes"],
            [None, None, None, None, None],
            ["Borg Valves", "", "NO", "B-7", "conditional"],
        ]
    )
    book = read_spreadsheet(data, "list.xlsx", max_rows=100)
    sheet = book.sheet(None)
    assert sheet.name == "Vendors" and sheet.width == 5
    header_row = detect_header_row(sheet)
    assert header_row == 3
    names = headers(sheet, header_row)
    assert suggest_mapping(names) == {
        "name": 0,
        "website": 1,
        "country": 2,
        "internal_code": 3,
        "avl_status": 4,
    }
    rows, truncated = data_rows(sheet, header_row, limit=10)
    assert [n for n, _ in rows] == [4, 6] and not truncated
    assert rows[0][1][3] == "1042"  # integral floats lose the ".0"
    rows, truncated = data_rows(sheet, header_row, limit=1)
    assert len(rows) == 1 and truncated


def test_reads_csv_with_semicolons_and_bom() -> None:
    data = b"\xef\xbb\xbfCompany;Website;Products\r\nFjord AS;fjord.no;Pumps, Valves\r\n"
    sheet = read_spreadsheet(data, "v.CSV", max_rows=10).sheet(None)
    assert headers(sheet, 1) == ["Company", "Website", "Products"]
    assert sheet.rows[1][2] == "Pumps, Valves"
    assert sheet.rows[1][:2] == ["Fjord AS", "fjord.no"]


def test_rejects_unsupported_and_corrupt_files() -> None:
    with pytest.raises(ValidationFailed, match="Unsupported"):
        read_spreadsheet(b"x", "old.xls", max_rows=10)
    with pytest.raises(ValidationFailed, match="readable"):
        read_spreadsheet(b"not a zip", "bad.xlsx", max_rows=10)
    with pytest.raises(ValidationFailed, match="no sheet named"):
        read_spreadsheet(xlsx([["a", "b"]]), "a.xlsx", max_rows=10).sheet("Missing")


def test_suggest_mapping_prefers_exact_headers_and_falls_back_to_words() -> None:
    names = ["Contact Name", "Vendor Name", "Main Products / Equipment", "ISO Certifications", "Remarks"]
    assert suggest_mapping(names) == {"name": 1, "equipment": 2, "certifications": 3, "description": 4}


def test_list_avl_and_term_helpers() -> None:
    assert split_list("Pumps; Valves | Compressors\nPumps") == ["Pumps", "Valves", "Compressors"]
    assert split_list("1,000 bar, Seals") == ["1,000 bar", "Seals"]
    assert parse_avl(" Not-Approved ") == AvlStatus.NOT_APPROVED
    assert parse_avl("Y") == AvlStatus.APPROVED and parse_avl("maybe") is None
    assert _term_key("ISO 9001:2015") == _term_key("ISO_9001")
    assert _term_key("Centrifugal Pumps") == _term_key("centrifugal pump")
    assert _csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _csv_safe("Acme") == "Acme"


def _taxonomy() -> Taxonomy:
    tax = Taxonomy()
    tax.equipment.add("pumps.centrifugal", "pumps.centrifugal", "Centrifugal pumps")
    tax.equipment.add("compressors.centrifugal", "compressors.centrifugal", "Centrifugal compressors")
    tax.certifications.add("ISO_9001", "ISO_9001", "ISO 9001 Quality management")
    tax.industries.add("lng", "lng", "LNG")
    return tax


def test_parse_row_normalises_and_collects_warnings() -> None:
    mapping = {
        "name": 0,
        "website": 1,
        "country": 2,
        "equipment": 3,
        "certifications": 4,
        "avl_status": 5,
        "tags": 6,
        "aliases": 7,
    }
    values = [
        "Acme Pumps Ltd",
        "http://WWW.Acme-Pumps.co.uk/en",
        "Atlantis",
        "Centrifugal Pumps; Widgets",
        "ISO 9001:2015",
        "blacklist",
        "rotating",
        "Acme; ACME Pumps Limited; AP",
    ]
    row = parse_row(9, values, mapping, _taxonomy(), ImportOptions(tags=["import-2026"]))
    assert row.error is None and row.row_number == 9
    assert row.domain == "acme-pumps.co.uk" and row.country is None
    assert row.equipment == [("pumps.centrifugal", "Centrifugal Pumps")]
    assert row.certifications == ["ISO_9001"]
    assert row.aliases == ["Acme", "AP"]  # "ACME Pumps Limited" normalises to the vendor's own name
    assert row.overlay == {"avl_status": "blacklisted", "tags": ["import-2026", "rotating"]}
    assert any("Atlantis" in m for m in row.messages) and any("Widgets" in m for m in row.messages)


def test_parse_row_errors_and_defaults() -> None:
    tax = _taxonomy()
    assert (
        parse_row(2, ["", "x.com"], {"name": 0, "website": 1}, tax, ImportOptions()).error == "No vendor name"
    )
    row = parse_row(
        3,
        ["Borg", "not a site", "maybe"],
        {"name": 0, "website": 1, "avl_status": 2},
        tax,
        ImportOptions(default_avl_status=AvlStatus.UNASSESSED),
    )
    assert row.website is None and row.overlay == {"avl_status": "unassessed"}
    assert len(row.messages) == 2
