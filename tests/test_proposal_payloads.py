"""Pure unit tests for proposal item payload validation."""

import pytest

from app.core.exceptions import ValidationFailed
from app.domain.enums import ProposalItemAction as A
from app.modules.proposals.payloads import validate_evidence, validate_payload


def test_create_vendor_normalises_website_and_country() -> None:
    p = validate_payload(
        A.CREATE_VENDOR, {"legal_name": " Flowserve Corp ", "website": "flowserve.com/", "hq_country": "USA"}
    )
    assert p["legal_name"] == "Flowserve Corp"
    assert p["website"] == "https://flowserve.com"
    assert p["hq_country"] == "US"


@pytest.mark.parametrize(
    ("action", "payload", "field"),
    [
        (A.CREATE_VENDOR, {}, "payload.legal_name"),
        (A.CREATE_VENDOR, {"legal_name": "X", "website": "not a site"}, "payload.website"),
        (A.CREATE_VENDOR, {"legal_name": "X", "hq_country": "Atlantis"}, "payload.hq_country"),
        (A.CREATE_VENDOR, {"legal_name": "X", "unexpected": 1}, "payload.unexpected"),
        (A.ADD_DOMAIN, {"domain": "localhost"}, "payload.domain"),
        (A.ADD_EQUIPMENT, {"category_code": "pumps"}, "payload.name"),
        (A.UPDATE_VENDOR_FIELD, {"field": "legal_name", "value": " "}, "payload"),
        (A.UPDATE_VENDOR_FIELD, {"field": "status", "value": "retired"}, "payload.field"),
        (
            A.ADD_CERTIFICATION,
            {"certification_code": "ISO_9001", "valid_from": "2026-05-01", "valid_to": "2025-01-01"},
            "payload",
        ),
    ],
)
def test_invalid_payloads(action: A, payload: dict[str, object], field: str) -> None:
    with pytest.raises(ValidationFailed) as err:
        validate_payload(action, payload)
    assert any(e["field"] == field for e in err.value.errors or []), err.value.errors


def test_update_field_normalises() -> None:
    assert (
        validate_payload(A.UPDATE_VENDOR_FIELD, {"field": "hq_country", "value": "Norway"})["value"] == "NO"
    )
    assert (
        validate_payload(A.UPDATE_VENDOR_FIELD, {"field": "website", "value": "ksb.com"})["value"]
        == "https://ksb.com"
    )


def test_add_domain_reduces_to_registrable_domain() -> None:
    assert validate_payload(A.ADD_DOMAIN, {"domain": "https://pumps.ksb.co.uk/x"})["domain"] == "ksb.co.uk"


def test_evidence_requires_url_or_title() -> None:
    ok = validate_evidence([{"url": "https://example.com/pump", "snippet": "BB2 pumps", "kind": "web"}])
    assert ok[0]["url"] == "https://example.com/pump"
    with pytest.raises(ValidationFailed):
        validate_evidence([{"snippet": "no reference"}])
