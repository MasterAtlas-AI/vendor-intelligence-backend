"""Equipment category from MEL descriptions (no database): the item named last wins, asides and accessories don't
count, and one-word equipment terms work on short descriptions. Cases are real FPSO MEL lines."""

import pytest

from app.modules.equipment.classify import classify
from app.modules.imports.pdf_extract import PhraseIndex
from app.modules.taxonomy.seed_data import EQUIPMENT, Node


def _flatten(
    nodes: tuple[Node, ...], prefix: str, out: list[tuple[str, list[str]]]
) -> list[tuple[str, list[str]]]:
    for node in nodes:
        code = f"{prefix}.{node.key}" if prefix else node.key
        out.append((code, [node.name, *node.synonyms]))
        _flatten(node.children, code, out)
    return out


def _labels() -> tuple[PhraseIndex, set[str]]:
    labels = _flatten(EQUIPMENT, "", [])
    return PhraseIndex({"equipment": labels}), {code for code, _ in labels}


INDEX, KNOWN = _labels()


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        # the item named last
        (["No.1 Ballast Pump Turbine Priming L.O. Pump Motor"], "electrical.motors"),
        (["No.1 Ballast Pump Turbine Priming L.O. Cooler"], "heat_transfer"),
        (["Emergency Diesel Generator Day Tank"], "static.atmospheric_vessels"),
        (["Essential Diesel Generator Package"], "electrical.generators"),
        (["Reverse Osmosis Fresh Water Generator No.2"], "utility_packages.fresh_water"),
        (["No.2  Seawater Lift Pump"], "pumps.centrifugal.seawater_lift"),
        (["Methanol Tank Cofferdam Eductor"], "pumps"),
        # a generic head noun keeps the kind the text names
        (["BB2 pump", "Seawater lift pump"], "pumps.centrifugal.api610.bb2"),
        (["Fire Water Pump Motor"], "electrical.motors"),
        # asides, accessories and locations don't name the item
        (["No.1  Forced Draft Fan with Motor"], "fans_blowers"),
        (["Fan Cooled Unit (UPS Room)"], "packages.hvac"),
        (["Foam Deluge Valve Cabinet - Cargo Main Deck Below M20"], "safety.fire_fighting"),
        (["Electro - Hydraulic Steering Gear Main Pump Motor No.1"], "electrical.motors"),
        (["Gas Lift Compressor Package - Train A"], "packages.gas_compression"),
        # package wording
        (["Subsea Chemical Injection System Skid A"], "packages.chemical_injection"),
        (["Gas Dehydration Unit"], "packages.gas_dehydration"),
        (["No.1 COT (P) Deck Mounted Tank Cleaning Machine"], "offshore_marine.marine_equipment"),
        (["No.2  Port Fender Davit"], "offshore_marine.marine_equipment.hull_equipment"),
        (["Port Lifeboat Davit"], "offshore_marine.lifeboats"),
        # the type column comes first; then description, service, section
        (
            ["Progressive Cavity Pump", "LP Flare Pumps"],
            "pumps.positive_displacement.rotary.progressive_cavity",
        ),
        (["Atm Tank", "Lab Drain Tank"], "static.atmospheric_vessels"),
        (["", "Bench Grinder", "", "WORKSHOP EQUIPMENT"], None),
        (["", "Radiator"], "heat_transfer"),
    ],
)
def test_classify(texts: list[str], expected: str | None) -> None:
    assert classify(INDEX, KNOWN, texts) == expected


def test_unknown_categories_are_never_returned() -> None:
    assert classify(INDEX, KNOWN - {"electrical.motors"}, ["Ballast Pump Motor"]) == "pumps"
