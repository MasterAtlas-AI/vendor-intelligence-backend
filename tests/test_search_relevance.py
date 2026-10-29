"""Search relevance on a fixed catalogue: 50 queries, nDCG@10 (Phase 4 exit criterion: >= 0.75).

The catalogue (36 vendors) and the relevance labels are synthetic and rule-based: a vendor is fully relevant (2)
when it has the requested equipment and every other requested property (certification, industry), partly
relevant (1) when it has the equipment only. Countries are hard filters. This measures the ranking logic (query
understanding, structured matching, scoring); it runs with the scripted provider, so it costs nothing. A
real-world check against labelled production data is a separate, manual exercise.

The search is limited to the organization's own vendor list ("my vendors"), so other test data in the database
can't affect the result.
"""

import math
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient

from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.domain.enums import ProposalItemAction
from app.modules.proposals.service import NewItem
from tests.conftest import Factory
from tests.test_search import _eq, _vendor

API = "/api/v1"

# (name, country, equipment codes, certification codes, industry codes)
CATALOGUE: list[tuple[str, str, list[str], list[str], list[str]]] = [
    ("Nordbarrel", "NO", ["pumps.centrifugal.api610.bb2"], ["API_610", "ISO_9001"], ["fpso", "offshore"]),
    ("Gulf Barrel Pumps", "AE", ["pumps.centrifugal.api610.bb2"], ["ISO_9001"], ["oil_gas_downstream"]),
    (
        "Indus Process Pumps",
        "IN",
        ["pumps.centrifugal.api610.bb2", "pumps.centrifugal.api610.oh2"],
        ["API_610"],
        ["petrochemical"],
    ),
    ("Rhein Pumpen", "DE", ["pumps.centrifugal.api610.oh2"], ["ISO_9001", "PED"], ["petrochemical"]),
    ("Texas OH Pumps", "US", ["pumps.centrifugal.api610.oh2"], [], ["oil_gas_downstream"]),
    ("Seawater Can Pumps", "NO", ["pumps.centrifugal.api610.vs6"], ["NORSOK"], ["offshore", "fpso"]),
    ("Deccan Vertical Pumps", "IN", ["pumps.centrifugal.api610.vs6"], [], ["water"]),
    ("FireGuard Pumps", "GB", ["pumps.centrifugal.firewater"], ["ISO_9001"], ["offshore"]),
    ("Mumbai Fire Systems", "IN", ["pumps.centrifugal.firewater"], [], ["petrochemical"]),
    ("Plunger Works", "DE", ["pumps.positive_displacement.reciprocating"], ["ATEX"], ["oil_gas_upstream"]),
    ("Ohio Plunger Pumps", "US", ["pumps.positive_displacement.reciprocating"], [], ["oil_gas_upstream"]),
    ("TurboComp", "IT", ["compressors.centrifugal"], ["ISO_9001"], ["lng", "petrochemical"]),
    ("Kansai Turbo", "JP", ["compressors.centrifugal"], [], ["lng"]),
    ("RecipCo", "DE", ["compressors.reciprocating"], ["ATEX"], ["oil_gas_midstream"]),
    ("Prairie Recip", "US", ["compressors.reciprocating"], [], ["oil_gas_midstream"]),
    (
        "Gujarat Recip",
        "IN",
        ["compressors.reciprocating", "packages.gas_compression"],
        ["ISO_9001"],
        ["oil_gas_midstream"],
    ),
    ("Trunnion Valve Co", "IT", ["valves.ball"], ["API_6D", "ISO_9001"], ["oil_gas_midstream"]),
    ("Bergen Ball Valves", "NO", ["valves.ball"], ["NORSOK", "API_6D"], ["offshore"]),
    ("Chennai Valves", "IN", ["valves.ball", "valves.gate"], [], ["petrochemical"]),
    ("GateTech", "GB", ["valves.gate"], ["API_600"], ["oil_gas_downstream"]),
    ("Stavanger Gate Valves", "NO", ["valves.gate"], ["NORSOK"], ["offshore"]),
    ("ControlFlow", "US", ["valves.control"], ["SIL"], ["lng"]),
    ("Alpine Control Valves", "DE", ["valves.control"], ["SIL", "PED"], ["petrochemical"]),
    ("Pune Control Systems", "IN", ["valves.control"], [], ["power"]),
    ("TEMA Exchangers", "GB", ["heat_transfer.shell_tube"], ["ASME_U"], ["oil_gas_downstream"]),
    ("Korea Shell Tube", "KR", ["heat_transfer.shell_tube"], ["ASME_U", "PED"], ["petrochemical"]),
    ("PlateHeat AB", "SE", ["heat_transfer.plate"], ["PED"], ["marine"]),
    ("Delhi Plate Exchangers", "IN", ["heat_transfer.plate"], [], ["power"]),
    ("Compression Skids", "CA", ["packages.gas_compression"], ["ISO_9001"], ["oil_gas_upstream"]),
    ("Desert Compression", "AE", ["packages.gas_compression"], [], ["oil_gas_upstream"]),
    ("Injection Systems", "NO", ["packages.water_injection"], ["NORSOK"], ["fpso", "offshore"]),
    ("Brazil WI Packages", "BR", ["packages.water_injection"], [], ["fpso"]),
    ("ExMotors", "DE", ["electrical.motors"], ["ATEX", "IECEX"], ["oil_gas_upstream"]),
    ("Bharat Motors", "IN", ["electrical.motors"], [], ["power"]),
    ("FlowMeter Inc", "US", ["instrumentation.flow"], ["SIL"], ["oil_gas_midstream"]),
    ("Nordic Flow Instruments", "NO", ["instrumentation.flow"], ["NORSOK"], ["offshore"]),
]

PHRASES = {
    "pumps.centrifugal.api610.bb2": "BB2 pumps",
    "pumps.centrifugal.api610.oh2": "OH2 pumps",
    "pumps.centrifugal.api610.vs6": "VS6 pumps",
    "pumps.centrifugal.firewater": "firewater pumps",
    "pumps.positive_displacement.reciprocating": "reciprocating pumps",
    "compressors.centrifugal": "centrifugal compressors",
    "compressors.reciprocating": "reciprocating compressors",
    "valves.ball": "ball valves",
    "valves.gate": "gate valves",
    "valves.control": "control valves",
    "heat_transfer.shell_tube": "shell and tube heat exchangers",
    "heat_transfer.plate": "plate heat exchangers",
    "packages.gas_compression": "gas compression packages",
    "packages.water_injection": "water injection packages",
    "electrical.motors": "electric motors",
    "instrumentation.flow": "flow meters",
}
COUNTRY_NAMES = {"NO": "Norway", "IN": "India", "DE": "Germany", "US": "United States", "AE": "UAE"}
CERT_PHRASES = {"API_6D": "API 6D", "NORSOK": "NORSOK", "ATEX": "ATEX", "SIL": "SIL", "PED": "PED",
                "ISO_9001": "ISO 9001", "ASME_U": "ASME U stamp"}  # fmt: skip
INDUSTRY_PHRASES = {
    "fpso": "for FPSO",
    "lng": "for LNG",
    "offshore": "for offshore",
    "petrochemical": "for petrochemical",
}


def queries() -> list[tuple[str, dict[str, Any]]]:
    """50 (query, constraints) pairs: by equipment; + country; + certification; + industry."""
    out: list[tuple[str, dict[str, Any]]] = [(f"{p} suppliers", {"eq": code}) for code, p in PHRASES.items()]
    for code, p in PHRASES.items():
        countries = sorted({c for _, c, eqs, _, _ in CATALOGUE if code in eqs and c in COUNTRY_NAMES})
        for c in countries[:1]:
            out.append((f"{p} in {COUNTRY_NAMES[c]}", {"eq": code, "country": c}))
    for code, p in PHRASES.items():
        certs = sorted(
            {ct for _, _, eqs, cts, _ in CATALOGUE if code in eqs for ct in cts if ct in CERT_PHRASES}
        )
        for ct in certs[:1]:
            out.append((f"{p} {CERT_PHRASES[ct]}", {"eq": code, "cert": ct}))
    for code, p in PHRASES.items():
        inds = sorted(
            {i for _, _, eqs, _, ins in CATALOGUE if code in eqs for i in ins if i in INDUSTRY_PHRASES}
        )
        for ind in inds[:1]:
            out.append((f"{p} {INDUSTRY_PHRASES[ind]}", {"eq": code, "industry": ind}))
    return out[:50]


def grade(vendor: tuple[str, str, list[str], list[str], list[str]], q: dict[str, Any]) -> int:
    _, country, eqs, certs, inds = vendor
    if q["eq"] not in eqs or ("country" in q and country != q["country"]):
        return 0
    extra_ok = ("cert" not in q or q["cert"] in certs) and ("industry" not in q or q["industry"] in inds)
    return 2 if extra_ok else 1


def ndcg(ranked: list[int], ideal: list[int], k: int = 10) -> float:
    def dcg(gains: list[int]) -> float:
        return sum(float(2**g - 1) / math.log2(i + 2) for i, g in enumerate(gains[:k]))

    best = dcg(sorted(ideal, reverse=True))
    return dcg(ranked) / best if best else 1.0


@pytest.fixture
def fake_ai(container: Container) -> Iterator[None]:
    previous, container.ai.provider = container.ai.provider, FakeProvider()
    yield
    container.ai.provider = previous


async def test_search_relevance(
    client: AsyncClient, factory: Factory, container: Container, fake_ai: None
) -> None:
    tag = uuid.uuid4().hex[:6]
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    ids: dict[str, tuple[str, str, list[str], list[str], list[str]]] = {}
    for vendor in CATALOGUE:
        name, country, eqs, certs, inds = vendor
        items = [_eq(code, PHRASES[code].split(" suppliers")[0].capitalize()) for code in eqs]
        items += [NewItem(ProposalItemAction.ADD_CERTIFICATION, {"certification_code": c}) for c in certs]
        items += [NewItem(ProposalItemAction.ADD_INDUSTRY, {"industry_code": i}) for i in inds]
        vid = await _vendor(
            container, f"{name} {tag}", country, items, website=f"v{len(ids)}-{tag}-maitest.com"
        )
        ids[str(vid)] = vendor
        await client.put(f"{API}/my-vendors/{vid}", json={"avl_status": "approved"}, headers=admin.headers)

    qs = queries()
    assert len(qs) == 50
    scores: list[tuple[float, str]] = []
    for n, (text, q) in enumerate(qs):
        if n % 10 == 0:  # seeding over a remote test database can outlast a 15-minute access token
            admin = await factory.user(tenant, ["tenant_admin"])
        resp = await client.post(f"{API}/search", headers=admin.headers,
                                 json={"query": text, "limit": 10, "filters": {"my_vendors_only": True}})  # fmt: skip
        assert resp.status_code == 200, (text, resp.text)
        ranked = [
            grade(ids[h["vendor"]["id"]], q) for h in resp.json()["results"] if h["vendor"]["id"] in ids
        ]
        ideal = [g for v in CATALOGUE if (g := grade(v, q))]
        scores.append((ndcg(ranked, ideal), text))

    mean = sum(s for s, _ in scores) / len(scores)
    worst = sorted(scores)[:5]
    print(f"\nnDCG@10 over {len(scores)} queries: {mean:.3f}; weakest: {worst}")
    assert mean >= 0.75, f"nDCG@10 {mean:.3f} < 0.75; weakest: {worst}"
