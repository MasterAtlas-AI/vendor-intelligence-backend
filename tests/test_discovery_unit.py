"""Vendor discovery logic without AI calls or a database: consolidation, the evidence rule, turning an extraction into
proposal input, classification and cost estimates. Uses the real seed taxonomy, built in memory."""

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from openai.lib._pydantic import to_strict_json_schema

from app.ai.ports import Citation, LLMResult, Usage
from app.core.config import Settings
from app.crawler.crawler import Page
from app.discovery.classify import classify, local_code
from app.discovery.consolidate import consolidate
from app.discovery.estimate import estimate
from app.discovery.evidence import EvidenceCheck
from app.discovery.pipeline import Researched, build_row, criteria_for, page_payload, pending_terms
from app.discovery.schemas import (
    Classification,
    DiscoveryRequest,
    FactEvidence,
    FoundCompany,
    SearchFindings,
    VendorExtraction,
)
from app.domain.enums import ProposalItemAction
from app.modules.imports.importer import Taxonomy, _fact_key
from tests.pdf_factory import seed_labels

SITE = "https://fjordpumps-maitest.com"
PRODUCTS = f"{SITE}/products/bb2"
PRODUCTS_TEXT = (
    "Our HX-2 is an API 610 BB2 between-bearings pump for water injection on FPSOs. "
    "Fjord Pumps AS holds ISO 9001:2015 certificate no. QA-778 and serves offshore oil & gas."
)


def seed_taxonomy() -> Taxonomy:
    tax = Taxonomy()
    vocabs = {"equipment": tax.equipment, "services": tax.services, "certifications": tax.certifications,
              "industries": tax.industries}  # fmt: skip
    for kind, entries in seed_labels().items():
        for code, labels in entries:
            vocabs[kind].add(code, code, *labels)
            tax.ids[(kind, code)] = uuid.uuid4()
            tax.names[(kind, code)] = labels[0]
            tax.labels.setdefault(kind, []).append((code, labels))
    return tax


TAX = seed_taxonomy()


def company(
    name: str, website: str | None, kind: str = "manufacturer", relevance: float = 0.8, **kw: Any
) -> FoundCompany:
    return FoundCompany(name=name, website=website, country=kw.get("country"), kind=kind, why=kw.get("why", "x"),
                        relevance=relevance)  # type: ignore[arg-type]  # fmt: skip


def test_ai_output_schemas_are_strict_mode_compatible() -> None:
    for schema in (SearchFindings, VendorExtraction, Classification):
        to_strict_json_schema(schema)


def test_consolidate_merges_ranks_and_filters() -> None:
    findings = [
        (
            [
                company(
                    "Fjord Pumps AS",
                    "https://www.fjordpumps-maitest.com/en/",
                    relevance=0.7,
                    country="Norway",
                ),
                company("Pump Directory", "https://www.thomasnet.com/pumps", kind="directory_or_marketplace"),
                company("Weak Match Ltd", "https://weak-maitest.com", relevance=0.2),
                company("Nordic Distributors", "https://nordist-maitest.com", kind="distributor"),
            ],
            [Citation(f"{SITE}/products/bb2", "BB2"), Citation("https://other-maitest.com/x")],
        ),
        (
            [
                company("FJORD PUMPS", "fjordpumps-maitest.com", relevance=0.9),
                company("Bergen Seals AS", None),
            ],
            [],
        ),
    ]
    kept, dropped = consolidate(findings, limit=5)
    assert [c.name for c in kept] == ["Fjord Pumps AS", "Bergen Seals AS"]
    fjord = kept[0]
    assert fjord.mentions == 2 and fjord.relevance == 0.9 and fjord.country == "NO"
    assert fjord.domain == "fjordpumps-maitest.com" and [s.url for s in fjord.sources] == [
        f"{SITE}/products/bb2"
    ]
    reasons = {c.name: c.discarded for c in dropped}
    assert "isn't one" in reasons["Pump Directory"]
    assert reasons["Weak Match Ltd"] == "Low relevance to the request"
    assert reasons["Nordic Distributors"].startswith("Distributor only")
    kept, dropped = consolidate(findings, limit=1)
    assert [c.name for c in kept] == ["Fjord Pumps AS"] and any(
        c.discarded and "first 1" in c.discarded for c in dropped
    )


def test_evidence_rule() -> None:
    check = EvidenceCheck.build([(PRODUCTS, "BB2 pumps", PRODUCTS_TEXT)])
    good = FactEvidence(url=PRODUCTS + "?utm_source=x", quote="an  API 610 BB2 between-bearings PUMP")
    assert check.verify([good]) == [{"kind": "web", "url": PRODUCTS, "title": "BB2 pumps",
                                     "snippet": "an  API 610 BB2 between-bearings PUMP"}]  # fmt: skip
    assert (
        check.verify([FactEvidence(url=f"{SITE}/never-fetched", quote="API 610 BB2 between-bearings")]) == []
    )
    assert (
        check.verify([FactEvidence(url=PRODUCTS, quote="API 682 dual seals with plan 53B")]) == []
    )  # not on page
    assert check.verify([FactEvidence(url=PRODUCTS, quote="pump")]) == []  # too short to prove anything


def _extraction(**overrides: Any) -> VendorExtraction:
    data: dict[str, Any] = {
        "is_relevant": True, "relevance_reason": "Makes API 610 BB2 pumps", "legal_name": "Fjord Pumps AS",
        "hq_country": "NO", "hq_city": "Bergen", "description": "Norwegian pump maker.",
        "equipment": [
            {"name": "HX-2 BB2 pump", "category_hint": "BB2 between bearings pump", "model_series": "HX-2",
             "standards": ["API 610"], "confidence": 0.9,
             "evidence": [{"url": PRODUCTS, "quote": "Our HX-2 is an API 610 BB2 between-bearings pump"}]},
            {"name": "Mystery pump", "category_hint": "centrifugal pump", "model_series": None, "standards": [],
             "confidence": 0.9, "evidence": [{"url": PRODUCTS, "quote": "a quote that is not on the page at all"}]},
        ],
        "services": [],
        "certifications": [{"name": "ISO 9001:2015", "certificate_number": "QA-778", "confidence": 0.85,
                            "evidence": [{"url": PRODUCTS, "quote": "holds ISO 9001:2015 certificate no. QA-778"}]}],
        "industries": [{"name": "Offshore", "confidence": 0.4,
                        "evidence": [{"url": PRODUCTS, "quote": "serves offshore oil & gas"}]}],
        "projects": [],
    }  # fmt: skip
    data.update(overrides)
    return VendorExtraction.model_validate(data)


def _researched(extraction: VendorExtraction | None, vendor_id: Any = None) -> Researched:
    cand = consolidate([([company("Fjord Pumps", SITE + "/", relevance=0.9)], [])], limit=5)[0][0]
    page = Page(PRODUCTS, PRODUCTS, 200, "text/html", title="BB2 pumps", text=PRODUCTS_TEXT)
    return Researched(cand, vendor_id=vendor_id, pages=[page], extraction=extraction)


def test_build_row_keeps_only_verified_classified_facts() -> None:
    r = _researched(_extraction())
    check = EvidenceCheck.build([(PRODUCTS, "BB2 pumps", PRODUCTS_TEXT)])
    row, stats = build_row(r, TAX, {}, check)
    assert (
        row.name == "Fjord Pumps AS"
        and row.country == "NO"
        and row.city == "Bergen"
        and row.discarded is None
    )
    assert row.equipment == [("pumps.centrifugal.api610.bb2", "HX-2 BB2 pump")]
    eq_key = _fact_key(ProposalItemAction.ADD_EQUIPMENT, {"category_code": "pumps.centrifugal.api610.bb2"})
    assert row.evidence[eq_key][0]["url"] == PRODUCTS and row.confidence[eq_key] == Decimal("0.9")
    assert row.extra[eq_key] == {"model_series": "HX-2", "standards": ["API 610"]}
    assert row.certifications == ["ISO_9001"]
    cert_key = _fact_key(ProposalItemAction.ADD_CERTIFICATION, {"certification_code": "ISO_9001"})
    assert row.extra[cert_key] == {"certificate_number": "QA-778"}
    assert row.industries == []  # confidence 0.4 is below the bar
    assert stats == {"found": 4, "kept": 2, "unsupported": 2, "unmapped": 0}
    assert row.default_evidence and row.default_evidence[0]["url"] == PRODUCTS
    assert row.details["highlights"] == {"equipment": ["HX-2 BB2 pump"], "certifications": ["ISO 9001:2015"]}


def test_build_row_discards_irrelevant_and_unsupported_new_vendors() -> None:
    check = EvidenceCheck.build([(PRODUCTS, "BB2 pumps", PRODUCTS_TEXT)])
    row, _ = build_row(
        _researched(_extraction(is_relevant=False, relevance_reason="Only sells valves")), TAX, {}, check
    )
    assert row.discarded == "Not a match: Only sells valves"
    empty = _extraction(equipment=[], certifications=[], industries=[])
    row, _ = build_row(_researched(empty), TAX, {}, check)
    assert row.discarded is not None and "verify" in row.discarded
    row, _ = build_row(_researched(empty, vendor_id=uuid.uuid4()), TAX, {}, check)
    assert row.discarded is None  # an existing vendor with nothing new is simply "matched"


def test_classification_local_first_then_ai_with_validation() -> None:
    # Phrases inside the text resolve without AI when they point down one branch ("BB2" -> api610.bb2).
    assert local_code(TAX, "equipment", "BB2 between bearings pump", "x") == "pumps.centrifugal.api610.bb2"
    assert local_code(TAX, "equipment", "BB2 and Ball valves") is None  # two branches: let the AI decide
    assert local_code(TAX, "equipment", "Centrifugal pumps") == "pumps.centrifugal"
    assert local_code(TAX, "certifications", "ISO 9001") == "ISO_9001"
    ex = _extraction(services=[{"name": "Pump overhaul", "category_hint": "pump repair", "confidence": 0.9,
                                "evidence": []}])  # fmt: skip
    pending = pending_terms(_researched(ex), TAX)
    assert ("services", "pump repair") in pending
    assert all(kind != "equipment" for kind, _ in pending)  # resolved locally, no AI needed


async def test_classify_drops_invented_or_wrong_kind_codes() -> None:
    class StubAi:
        async def run(self, prompt: Any, schema: Any, call: Any, **inputs: Any) -> LLMResult[Classification]:
            assert "pumps.centrifugal.api610.bb2" in inputs["taxonomy"]
            out = Classification.model_validate({"terms": [
                {"term": "equipment: BB2 between bearings pump", "code": "pumps.centrifugal.api610.bb2"},
                {"term": "services: pump repair", "code": "pumps.made_up"},
                {"term": "services: commissioning", "code": "pumps.centrifugal"},  # equipment code for a service
            ]})  # fmt: skip
            return LLMResult(out, Usage("m"))

    pending = [
        ("equipment", "BB2 between bearings pump"),
        ("services", "pump repair"),
        ("services", "commissioning"),
    ]
    codes = await classify(pending, TAX, StubAi(), SimpleNamespace())  # type: ignore[arg-type]
    assert codes == {("equipment", "BB2 between bearings pump"): "pumps.centrifugal.api610.bb2"}


def test_request_criteria_payload_and_estimate() -> None:
    req = DiscoveryRequest(equipment_categories=["pumps.centrifugal.api610.bb2"], industries=["fpso"],
                           countries=["Norway"], regions=["middle_east"], standards=["API 610"],
                           brief="Water injection", max_candidates=10)  # fmt: skip
    crit = criteria_for(req, TAX)
    assert crit["equipment"] == ["BB2 - Between bearings, radially split, 1-2 stage"]
    assert crit["industries"] == ["FPSO & floating production"] and crit["regions"] == ["middle east"]
    assert crit["countries"] == ["NO"] and "services" not in crit

    pages = [Page(f"{SITE}/{i}", f"{SITE}/{i}", 200, text="x" * 30_000) for i in range(5)]
    payload = page_payload(pages, budget=40_000)
    assert sum(len(p["text"]) for p in payload) <= 40_000 and payload[0]["url"] == f"{SITE}/0"

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    small, big = estimate(req, s), estimate(req.model_copy(update={"max_candidates": 25}), s)
    assert small.queries == 5 and small.web_searches == 20 and small.max_cost_usd > small.typical_cost_usd > 0
    assert big.typical_cost_usd > small.typical_cost_usd and big.queries == 10


def test_request_validation() -> None:
    with pytest.raises(ValueError, match="at least one"):
        DiscoveryRequest(industries=["fpso"])
    with pytest.raises(ValueError, match="unknown country"):
        DiscoveryRequest(brief="pumps", countries=["Atlantis"])
    with pytest.raises(ValueError):
        DiscoveryRequest(brief="pumps", max_candidates=100)
