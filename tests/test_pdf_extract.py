"""Rule-based PDF extraction (no database): text reading, tokenising, taxonomy matching, certificate details."""

from datetime import date
from decimal import Decimal

import pytest

from app.core.exceptions import ValidationFailed
from app.modules.imports.pdf_extract import PhraseIndex, extract, parse_date, read_pdf, tokens
from tests.pdf_factory import make_pdf, seed_labels

BROCHURE = [
    [
        "Nordic Flow Systems AS",
        "www.nordicflow-maitest.com  sales@nordicflow-maitest.com",
        "We design API 610 BB2 and BB5 barrel pumps for FPSO and LNG service.",
        "Vertical turbine pumps (VS1) for seawater lift. Power and water industries welcome.",
    ],
    [
        "Quality: ISO 9001:2015 certified, Certificate No. QA-12345-NO valid until 31.03.2027.",
        "ATEX and IECEx compliant motors. Nordic Flow Systems AS, Bergen. Partner: www.partner-maitest.com",
    ],
]


@pytest.fixture(scope="module")
def index() -> PhraseIndex:
    return PhraseIndex(seed_labels())


def test_tokens_glue_designations_and_fold_plurals() -> None:
    assert [t for t, _ in tokens("BB-2, BB 2 and bb2 Pumps; ISO_9001")] == [
        "bb2", "bb2", "and", "bb2", "pump", "iso9001",
    ]  # fmt: skip
    assert tokens("Valves")[0] == ("valve", 0)


def test_extracts_facts_with_evidence(index: PhraseIndex) -> None:
    pdf = read_pdf(make_pdf(BROCHURE), max_pages=50)
    assert len(pdf.pages) == 2 and pdf.page_count == 2 and not pdf.scanned and not pdf.truncated
    ex = extract(pdf, index)
    found = {(h.kind, h.code): h for h in ex.hits}

    # Specific pump types; their parents ("pumps.centrifugal.api610") are implied and dropped.
    equipment = {c for k, c in found if k == "equipment"}
    assert {
        "pumps.centrifugal.api610.bb2",
        "pumps.centrifugal.api610.bb5",
        "pumps.centrifugal.api610.vs1",
    } <= equipment
    assert "pumps.centrifugal.api610" not in equipment and "pumps.centrifugal" not in equipment
    assert found[("equipment", "pumps.centrifugal.api610.bb5")].occurrences == 2  # "BB5" and "barrel pump"

    iso = found[("certifications", "ISO_9001")]
    assert iso.page == 2 and "ISO 9001:2015" in iso.snippet
    assert iso.details == {"certificate_number": "QA-12345-NO", "valid_to": "2027-03-31"}
    assert iso.confidence == Decimal("0.75")
    assert {("certifications", "ATEX"), ("certifications", "IECEX")} <= set(found)

    # Acronym industries match; generic single words ("Power", "water") don't.
    industries = {c for k, c in found if k == "industries"}
    assert industries == {"fpso", "lng"}

    assert ex.domains[0] == ("nordicflow-maitest.com", 2)
    assert ("partner-maitest.com", 1) in ex.domains
    assert ex.names[0] == ("Nordic Flow Systems AS", 2)


def test_page_limit_and_scanned_detection(index: PhraseIndex) -> None:
    pdf = read_pdf(make_pdf([["ISO 9001"], ["API Q1"], ["ATEX"]]), max_pages=2)
    assert pdf.truncated and pdf.page_count == 3
    assert {h.code for h in extract(pdf, index).hits} == {"ISO_9001", "API_Q1"}
    assert read_pdf(make_pdf([[""], [""]]), max_pages=10).scanned


def test_rejects_unreadable_pdf() -> None:
    with pytest.raises(ValidationFailed, match="readable PDF"):
        read_pdf(b"%PDF-1.4 garbage", max_pages=10)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2027-03-31 and more", date(2027, 3, 31)),
        ("31.03.2027", date(2027, 3, 31)),
        ("1/12/2026", date(2026, 12, 1)),
        ("31st March, 2027", date(2027, 3, 31)),
        ("31 Foo 2027", None),
        ("31.02.2027", None),
        ("soon", None),
    ],
)
def test_parse_date(text: str, expected: date | None) -> None:
    assert parse_date(text) == expected


def test_company_names_may_contain_numbers(index: PhraseIndex) -> None:
    pdf = read_pdf(
        make_pdf([["Nordic 7fe578 Flow AS", "Made by Nordic 7fe578 Flow AS, Bergen"]]), max_pages=5
    )
    assert extract(pdf, index).names[0] == ("Nordic 7fe578 Flow AS", 2)
