"""Equipment category from a MEL line's short text ("No.1 Ballast Pump Turbine L.O. Cooler", "Atm Tank").

Equipment descriptions name the item last: "Emergency Diesel Generator Day Tank" is a tank, "Ballast Pump Motor" is a
motor. So among everything the text names - taxonomy labels and the common equipment words below - the phrase that
ends last wins (then the longer one, then the more specific category).

The one-word rules ("pump", "motor", "tank") are only safe on short equipment descriptions, which is why they live
here and not in the taxonomy labels used to read free-text PDFs. Local and rule-based: no AI call.
"""

import re
from collections.abc import Iterable

from app.modules.imports.pdf_extract import PhraseIndex

_P = r"(?:es|s)?"  # plural


def _rx(*words: str) -> re.Pattern[str]:
    """A rule's phrases, found at every position (a lookahead, so "Foam Deluge Valve Cabinet" yields both "Foam
    Deluge" and "Deluge Valve Cabinet"); the phrase is group 1."""
    return re.compile(r"(?=\b(" + "|".join(words) + r")\b)", re.I)


# (pattern, category code), most specific first: on an exact tie (same span) the earlier rule wins.
RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # ---- pumps
    (
        _rx(r"fire ?(?:water|main)? pumps?", r"jockey pumps?", r"emergency fire pumps?"),
        "pumps.centrifugal.firewater",
    ),
    (_rx(r"(?:sea ?water|sw) lift pumps?", r"caisson pumps?"), "pumps.centrifugal.seawater_lift"),
    (_rx(r"water injection (?:booster )?pumps?", r"wi pumps?"), "pumps.centrifugal.water_injection"),
    (_rx(r"submersible pumps?", r"deep ?well pumps?"), "pumps.centrifugal.submersible"),
    (_rx(r"(?:twin )?screw pumps?"), "pumps.positive_displacement.rotary.screw"),
    (_rx(r"gear pumps?"), "pumps.positive_displacement.rotary.gear"),
    (
        _rx(r"progress(?:ive|ing) cavity pumps?", r"pc pumps?", r"mono pumps?"),
        "pumps.positive_displacement.rotary.progressive_cavity",
    ),
    (
        _rx(r"(?:dosing|metering|chemical(?: injection)?|methanol(?: injection)?|inhibitor) pumps?"),
        "pumps.positive_displacement.metering",
    ),
    (_rx(r"(?:air[- ]operated )?diaphragm pumps?", r"aodd pumps?"), "pumps.positive_displacement.diaphragm"),
    (_rx(r"(?:reciprocating|plunger|piston) pumps?"), "pumps.positive_displacement.reciprocating"),
    (_rx(r"centrifugal pumps?"), "pumps.centrifugal"),
    (_rx(r"pumps?", r"eductors?", r"ejectors?"), "pumps"),
    # ---- drivers and power
    (
        _rx(
            r"(?:fresh ?water|fw) generators?",
            r"water makers?",
            r"reverse osmosis \w+",
            r"desalinat\w+",
        ),
        "utility_packages.fresh_water",
    ),
    (
        _rx(r"nitrogen (?:generat\w+|packages?|skids?|units?|systems?|membranes?)"),
        "packages.nitrogen_generation",
    ),
    (
        _rx(r"inert gas (?:generators?|plants?|packages?|systems?|units?|blowers?|scrubbers?)"),
        "offshore_marine.marine_packages.inert_gas",
    ),
    (
        _rx(
            r"steam generators?",
            r"waste heat recovery \w+",
            r"whrus?",
            r"boilers?",
            r"superheaters?",
            r"economi[sz]ers?",
            r"burners?",
        ),
        "heat_transfer.boilers",
    ),
    (_rx(r"\w*generators?", r"alternators?", r"gensets?", r"generating sets?"), "electrical.generators"),
    (_rx(r"motors?"), "electrical.motors"),
    (_rx(r"gas (?:or )?dual[- ]fuel engines?", r"gas engines?"), "turbines.gas_engine"),
    (_rx(r"engines?"), "turbines.diesel_engine"),
    (_rx(r"gas turbines?"), "turbines.gas_turbine"),
    (_rx(r"steam turbines?"), "turbines.steam_turbine"),
    (_rx(r"turbines?"), "turbines"),
    (_rx(r"gear ?box" + _P, r"reduction gears?", r"gear units?"), "rotating_aux.gears"),
    (_rx(r"couplings?"), "rotating_aux.couplings"),
    (_rx(r"bearings?"), "rotating_aux.bearings"),
    (
        _rx(
            r"lube oil (?:systems?|consoles?|packages?|units?|skids?|modules?)",
            r"l\.?o\.? (?:systems?|consoles?)",
        ),
        "rotating_aux.lube_oil",
    ),
    (
        _rx(r"mechanical seals?", r"seal (?:gas|oil|water) (?:systems?|panels?|consoles?|packages?|skids?)"),
        "rotating_aux.mechanical_seals",
    ),
    # ---- compressors
    (_rx(r"(?:starting |service |instrument |plant )?air compressors?"), "compressors.air"),
    (_rx(r"screw compressors?"), "compressors.screw"),
    (_rx(r"reciprocating compressors?"), "compressors.reciprocating"),
    (_rx(r"centrifugal compressors?"), "compressors.centrifugal"),
    (
        _rx(
            r"compressor (?:packages?|trains?|units?|skids?)",
            r"compression (?:packages?|trains?|systems?|units?)",
        ),
        "packages.gas_compression",
    ),
    (_rx(r"compressors?"), "compressors"),
    (
        _rx(
            r"(?:instrument )?air dryers?(?: packages?| skids?| units?)?",
            r"instrument air (?:packages?|skids?|units?)",
        ),
        "utility_packages.instrument_air_pkg",
    ),
    # ---- heat transfer
    (
        _rx(r"fin ?fan coolers?", r"air[- ]cooled (?:heat exchangers?|coolers?|condensers?)"),
        "heat_transfer.air_cooled",
    ),
    (_rx(r"plate (?:type )?(?:heat exchangers?|coolers?|heaters?)", r"phes?"), "heat_transfer.plate"),
    (_rx(r"shell (?:and|&) tube \w+"), "heat_transfer.shell_tube"),
    (
        _rx(r"electric (?:process )?heaters?", r"immersion heaters?", r"heating elements?"),
        "heat_transfer.electric_heaters",
    ),
    (_rx(r"fired heaters?", r"furnaces?"), "heat_transfer.fired_heaters"),
    (
        _rx(r"heating medium (?:heaters?|systems?|packages?|units?|expansion vessels?)", r"hot oil \w+"),
        "utility_packages.heating_medium",
    ),
    (
        _rx(r"cooling (?:water|medium) (?:packages?|units?|systems?|modules?|expansion tanks?)"),
        "utility_packages.cooling_water",
    ),
    (_rx(r"chillers?", r"chilled water \w+", r"condensing units?"), "packages.hvac"),
    (
        _rx(
            r"(?:heat )?exchangers?",
            r"coolers?",
            r"condensers?",
            r"radiators?",
            r"evaporators?",
            r"(?:pre-?)?heaters?",
            r"calorifiers?",
        ),
        "heat_transfer",
    ),
    # ---- static
    (
        _rx(
            r"(?:knock[- ]?out|ko) drums?",
            r"separators?",
            r"scrubbers?",
            r"slug catchers?",
            r"purifiers?",
            r"centrifuges?",
            r"hydrocyclones?",
            r"degassers?",
        ),
        "static.separators",
    ),
    (_rx(r"columns?", r"towers?", r"contactors?", r"absorbers?", r"deaerators?"), "static.columns"),
    (_rx(r"filters?", r"strainers?", r"coalescers?"), "static.filters"),
    (
        _rx(
            r"(?:air )?receivers?",
            r"accumulators?",
            r"hydrophores?",
            r"pressure (?:tanks?|vessels?)",
            r"drums?",
            r"vessels?",
            r"bottles?",
        ),
        "static.pressure_vessels",
    ),
    (_rx(r"(?:storage|cargo|slop|crude(?: oil)?) tanks?"), "static.storage_tanks"),
    (_rx(r"tanks?", r"reservoirs?", r"sumps?", r"hoppers?"), "static.atmospheric_vessels"),
    # ---- process and utility packages
    (
        _rx(
            r"chemical injection (?:system )?(?:skids?|packages?|units?|systems?)",
            r"(?:methanol|chemical) (?:injection )?(?:skids?|packages?|units?)",
            r"injection skids?",
        ),
        "packages.chemical_injection",
    ),
    (
        _rx(
            r"(?:gas )?dehydration (?:units?|packages?|systems?)",
            r"(?:hydrocarbon )?dew ?point (?:control )?(?:units?|packages?|skids?)",
            r"teg (?:units?|regeneration \w+|contactors?)",
            r"glycol (?:regeneration )?(?:units?|packages?)",
        ),
        "packages.gas_dehydration",
    ),
    (
        _rx(r"produced water (?:\w+ )?(?:packages?|units?|systems?)", r"sand (?:wash|jetting|removal) \w+"),
        "packages.produced_water",
    ),
    (
        _rx(
            r"water injection (?:\w+ )?(?:packages?|skids?|units?|systems?)",
            r"sulphate removal \w+",
            r"srus?",
        ),
        "packages.water_injection",
    ),
    (
        _rx(
            r"heli ?fuel \w+",
            r"f\.?\s?o\.?\s?(?:booster|supply)(?: units?| modules?)?",
            r"aviation fuel \w+",
            # "Diesel Treatment Package", not "Diesel Generator Package"
            r"(?:diesel|fuel oil|fuel|mdo|hfo) (?:oil )?(?:treatment|transfer|filtration|purification|supply|"
            r"storage|conditioning|filter|booster|centrifuge) (?:packages?|modules?|skids?|units?)",
            r"(?:diesel|fuel oil|mdo|hfo) (?:packages?|modules?|skids?|units?)",
        ),
        "utility_packages.diesel_system",
    ),
    (
        _rx(r"potable water \w+", r"(?:uv )?sterili[sz]ers?", r"mineraliz\w+ \w*"),
        "utility_packages.potable_water",
    ),
    (
        _rx(
            r"mgps",
            r"(?:\w+ )?hypochlorination \w+",
            r"marine growth \w+",
            r"anti-?fouling \w*",
            r"electro-?chlorinat\w+",
            r"hypochlorite\w* ?\w*",
        ),
        "utility_packages.cooling_water",
    ),
    (
        _rx(r"hydraulic power (?:units?|packs?)", r"hpus?", r"hydraulic (?:units?|packs?|power packs?)"),
        "mechanical_packages.hpu",
    ),
    (
        _rx(
            r"metering (?:skids?|packages?|systems?|units?|stations?)",
            r"fiscal metering \w*",
            r"\w+ metering",
        ),
        "mechanical_packages.metering",
    ),
    (_rx(r"pig (?:launchers?|receivers?|traps?)"), "mechanical_packages.pig_systems"),
    (
        _rx(
            r"ahus?",
            r"air handling units?",
            r"fcus?",
            r"fan coil units?",
            r"hvac \w*",
            r"air ?cond\w*",
            r"dehumidifiers?",
            r"refrigerat\w+(?: plants?| units?| packages?| machines?)?",
            r"package units?",
            r"ducts?",
            r"dampers?",
        ),
        "packages.hvac",
    ),
    (_rx(r"fan coil(?:ed)? units?", r"fan cooled units?"), "packages.hvac"),
    (_rx(r"fans?", r"blowers?", r"ventilators?", r"supply ?fans?"), "fans_blowers"),
    # ---- marine and lifting
    (
        _rx(
            r"offloading (?:hoses?|hose reels?|hose assembl(?:y|ies)|\w+)",
            r"(?:mooring )?hawsers?(?: reels?| assembl(?:y|ies))?",
            r"hose reels?",
        ),
        "offshore_marine.offloading",
    ),
    (_rx(r"tank (?:cleaning|washing) machines?", r"cow machines?"), "offshore_marine.marine_equipment"),
    (_rx(r"incinerators?", r"(?:garbage )?compactors?", r"comminutors?"), "offshore_marine.marine_equipment"),
    (
        _rx(r"sewage (?:treatment )?(?:plants?|units?|systems?|packages?)"),
        "offshore_marine.marine_packages.sewage",
    ),
    (
        _rx(
            r"oily (?:water|bilge) \w+",
            r"bilge (?:water )?separators?",
            r"oil content (?:meters?|monitors?)",
            r"ows",
        ),
        "offshore_marine.marine_packages.bilge",
    ),
    (_rx(r"ballast water treatment \w*", r"bwts"), "offshore_marine.marine_packages.ballast"),
    (_rx(r"cranes?"), "offshore_marine.cranes"),
    (
        _rx(
            r"davits?",
            r"life ?boats?",
            r"rescue boats?",
            r"fast rescue craft",
            r"life ?rafts?",
            r"rafts?",
            r"survival craft",
            r"life ?saving \w+",
        ),
        "offshore_marine.lifeboats",
    ),
    (
        _rx(r"hoists?", r"trolleys?", r"monorails?", r"chain blocks?", r"lifting \w+", r"gantr(?:y|ies)"),
        "mechanical_packages.lifting",
    ),
    (_rx(r"winch" + _P, r"windlass" + _P, r"capstans?"), "offshore_marine.marine_equipment.winches"),
    (_rx(r"steering gears?", r"rudders?"), "offshore_marine.marine_equipment.steering_gear"),
    (
        _rx(r"propellers?(?: shafts?)?", r"thrusters?", r"stern tubes?"),
        "offshore_marine.marine_equipment.thrusters",
    ),
    (
        _rx(
            r"iccp",
            r"impressed current \w+",
            r"cathodic \w+",
            r"fenders?(?: davits?)?",
            r"hatch(?:es| covers?)?",
            r"fairleads?",
            r"chain stoppers?",
            r"bollards?",
            r"anchors?",
            r"gangways?",
            r"(?:accommodation|pilot) ladders?",
        ),
        "offshore_marine.marine_equipment.hull_equipment",
    ),
    (_rx(r"risers?", r"umbilicals?"), "offshore_marine.risers_umbilicals"),
    (_rx(r"swivels?"), "offshore_marine.swivel"),
    # ---- safety
    (
        _rx(
            r"co2 (?:cylinders?|bottles?|systems?|packages?|skids?)",
            r"(?:\w+ )?fire extinguishing \w+",
            r"water mist \w*",
            r"deluge(?: valve)?(?: \w+)?",
            r"foam \w*",
            r"sprinklers?",
            r"clean agent \w*",
            r"fire hydrants?",
            r"fire monitors?",
        ),
        "safety.fire_fighting",
    ),
    (_rx(r"flare (?:tips?|stacks?|ignition \w+|booms?|packages?|systems?)"), "safety.flare"),
    (_rx(r"gas detect\w+", r"gas detectors?"), "safety.gas_detection"),
    (_rx(r"fire (?:and|&) gas \w+", r"f&g \w*"), "instrumentation.fire_gas"),
    # ---- electrical, control and telecom
    (_rx(r"transformers?"), "electrical.transformers"),
    (
        _rx(
            r"ups",
            r"uninterrupt\w+ power \w+",
            r"batter(?:y|ies)(?: chargers?| banks?)?",
            r"chargers?",
            r"rectifiers?",
            r"inverters?",
        ),
        "electrical.ups",
    ),
    (_rx(r"vfds?", r"vsds?", r"frequency converters?", r"variable speed drives?"), "electrical.vfd"),
    (
        _rx(
            r"switchboards?",
            r"switchgears?",
            r"mccs?",
            r"motor control cent\w+",
            r"distribution (?:boards?|panels?)",
            r"starter (?:panels?|cabinets?)",
            r"starters?",
            r"e-?houses?",
            r"electrical houses?",
            r"bus ducts?",
        ),
        "electrical.switchgear",
    ),
    (_rx(r"cables?", r"cable trays?"), "electrical.cables"),
    (
        _rx(
            r"control (?:panels?|cabinets?|consoles?|systems?|units?|stations?)",
            r"instrument (?:panels?|cabinets?)",
            r"plcs?",
            r"plc (?:cabinets?|panels?)",
            r"dcs",
            r"scada",
            r"telemetry \w+",
            r"alarm (?:panels?|systems?)",
        ),
        "instrumentation.dcs",
    ),
    (_rx(r"analy[sz]ers?"), "instrumentation.analyzers"),
    (
        _rx(
            r"(?:level|pressure) (?:gauges?|transmitters?|indicators?|switches)",
            r"tank gauging \w*",
            r"radar gauges?",
        ),
        "instrumentation.pressure",
    ),
    (_rx(r"flow ?meters?", r"meters?"), "instrumentation.flow"),
    (_rx(r"(?:psvs?|prvs?|(?:pressure )?relief valves?|safety valves?)"), "valves.safety_relief"),
    (_rx(r"actuators?"), "valves.actuators"),
    (_rx(r"valves?(?: skids?| units?)?"), "valves"),
    (_rx(r"cctv \w*", r"cameras?"), "telecom.cctv"),
    (_rx(r"public address \w*", r"general alarm \w*", r"pa ?/ ?ga", r"loudspeakers?"), "telecom.pa_ga"),
    (_rx(r"radios?", r"vhf \w*", r"uhf \w*", r"gmdss \w*", r"transceivers?"), "telecom.radio"),
    (_rx(r"vsat", r"satellite \w+", r"inmarsat \w*"), "telecom.satellite"),
    (_rx(r"telephones?", r"pabx", r"network (?:switches|racks?)"), "telecom.telephone"),
    # ---- piping
    (_rx(r"expansion joints?", r"bellows"), "piping.expansion_joints"),
    (_rx(r"hoses?", r"flexible (?:hoses?|pipes?|jumpers?)"), "piping.flexible_pipe"),
    (_rx(r"pipes?", r"piping", r"spools?"), "piping.pipe"),
    # ---- last resort: a panel or cabinet with nothing more specific is electrical
    (_rx(r"panels?", r"cabinets?", r"consoles?", r"junction box" + _P), "electrical"),
)


_ASIDE = re.compile(
    r"\([^)]*\)|\[[^\]]*\]"
)  # "(UPS Room)", "(Evaporator Type)": where or what kind, not what
_EXTRAS = re.compile(r"\s+(?:with|c/w|complete with|incl\.?|including|and its|for)\s+.*$", re.I)


def _subject(text: str) -> str:
    """The part of a description that names the item: "Forced Draft Fan with Motor" -> "Forced Draft Fan",
    "Drain Tank for Vacuum Pump" -> "Drain Tank"."""
    text = " ".join(_ASIDE.sub(" ", text).split())
    return _EXTRAS.sub("", text) or text


_DASH_TAIL = re.compile(r"\s+[-\u2013\u2014]\s+")


def classify(index: PhraseIndex, known: set[str], texts: Iterable[str]) -> str | None:
    """The category of the first text (type, description, service, section) that names one. A spaced dash usually
    adds where or which ("Deluge Valve Cabinet - Pipe Rack 03", "Compressor Package - Train A"), so the part before
    it is tried first."""
    for raw in texts:
        text = _subject(raw or "")
        head = _DASH_TAIL.split(text, maxsplit=1)[0]
        for candidate in dict.fromkeys((head, text)):
            code = _classify_one(index, known, candidate)
            if code:
                return code
    return None


def _classify_one(index: PhraseIndex, known: set[str], text: str) -> str | None:
    if not text:
        return None
    found: list[tuple[int, int, float, str]] = []  # (end, length, priority, code)
    for kind, code, start, end in index.find(text):
        if kind == "equipment":
            while end > start and not text[end - 1].isalnum():  # find() ends at the next word: trim back
                end -= 1
            found.append((end, end - start, 1000 + code.count("."), code))  # a taxonomy label beats a rule
    for rank, (pattern, code) in enumerate(RULES):
        if code in known:
            found += [(m.end(1), m.end(1) - m.start(1), -rank, code) for m in pattern.finditer(text)]
    if not found:
        return None
    best = max(found, key=lambda c: c[:3])
    # "BB2 pump": the head noun says pump, and the text also names which kind; keep the more specific one.
    narrower = [c for c in found if c[3].startswith(best[3] + ".")]
    return max(narrower, key=lambda c: (c[3].count("."), c[:3]))[3] if narrower else best[3]
