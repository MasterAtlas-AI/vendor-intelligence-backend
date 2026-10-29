"""Seed taxonomy for industrial procurement (Oil & Gas, FPSO, EPC, energy, process industries).

Codes are permanent identifiers (vendors' facts reference them); names and synonyms may be edited freely.
Synced idempotently by `app.cli bootstrap`. Add nodes here; never rename or remove a code that is in use.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    key: str
    name: str
    children: tuple["Node", ...] = ()
    standards: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    description: str = ""


def n(
    key: str, name: str, *children: Node, std: tuple[str, ...] = (), syn: tuple[str, ...] = (), desc: str = ""
) -> Node:
    return Node(key, name, children, std, syn, desc)


API610 = ("API 610", "ISO 13709")

EQUIPMENT: tuple[Node, ...] = (
    n(
        "pumps",
        "Pumps",
        n(
            "centrifugal",
            "Centrifugal pumps",
            n(
                "api610",
                "API 610 / ISO 13709 process pumps",
                n("oh1", "OH1 - Overhung, foot-mounted", std=API610),
                n(
                    "oh2",
                    "OH2 - Overhung, centreline-mounted",
                    std=API610,
                    syn=("OH-2", "centerline mounted pump"),
                ),
                n("oh3", "OH3 - Vertical in-line, separate bearing bracket", std=API610),
                n("oh4", "OH4 - Vertical in-line, rigidly coupled", std=API610),
                n("oh5", "OH5 - Vertical in-line, close-coupled", std=API610),
                n("oh6", "OH6 - High-speed integral gear", std=API610),
                n("bb1", "BB1 - Between bearings, axially split, 1-2 stage", std=API610),
                n("bb2", "BB2 - Between bearings, radially split, 1-2 stage", std=API610, syn=("BB-2",)),
                n("bb3", "BB3 - Between bearings, axially split, multistage", std=API610),
                n("bb4", "BB4 - Between bearings, single-casing ring-section multistage", std=API610),
                n(
                    "bb5",
                    "BB5 - Between bearings, double-casing (barrel) multistage",
                    std=API610,
                    syn=("barrel pump",),
                ),
                n(
                    "vs1",
                    "VS1 - Vertically suspended, wet pit diffuser",
                    std=API610,
                    syn=("vertical turbine pump", "VTP"),
                ),
                n("vs2", "VS2 - Vertically suspended, wet pit volute", std=API610),
                n("vs3", "VS3 - Vertically suspended, wet pit axial flow", std=API610),
                n("vs4", "VS4 - Vertically suspended, line-shaft sump", std=API610),
                n("vs5", "VS5 - Vertically suspended, cantilever sump", std=API610),
                n("vs6", "VS6 - Vertically suspended, double-casing diffuser", std=API610, syn=("can pump",)),
                n("vs7", "VS7 - Vertically suspended, double-casing volute", std=API610),
                std=API610,
                syn=("API pump", "refinery pump"),
            ),
            n("ansi", "ANSI / ISO 5199 chemical process pumps", std=("ASME B73.1", "ISO 5199")),
            n("split_case", "Split-case pumps", syn=("axially split pump", "horizontal split case")),
            n("submersible", "Submersible pumps", syn=("ESP", "electric submersible pump")),
            n("firewater", "Fire water pumps", std=("NFPA 20",), syn=("firewater pump",)),
            n("seawater_lift", "Seawater lift / caisson pumps", syn=("SWL pump", "caisson pump")),
            n("water_injection", "Water injection pumps", syn=("WI pump",)),
            n("cryogenic", "Cryogenic / LNG pumps", syn=("LNG send-out pump", "in-tank pump")),
            syn=("rotodynamic pump",),
        ),
        n(
            "positive_displacement",
            "Positive displacement pumps",
            n("reciprocating", "Reciprocating pumps", std=("API 674",), syn=("plunger pump", "piston pump")),
            n(
                "metering",
                "Metering / dosing pumps",
                std=("API 675",),
                syn=("chemical injection pump", "dosing pump"),
            ),
            n(
                "rotary",
                "Rotary pumps",
                n("screw", "Screw pumps", std=("API 676",), syn=("twin screw pump", "multiphase pump")),
                n("gear", "Gear pumps", std=("API 676",)),
                n("progressive_cavity", "Progressing cavity pumps", std=("API 676",), syn=("PC pump", "PCP")),
                std=("API 676",),
            ),
            n("diaphragm", "Diaphragm pumps", syn=("AODD", "air-operated diaphragm pump")),
        ),
    ),
    n(
        "compressors",
        "Compressors",
        n("centrifugal", "Centrifugal compressors", std=("API 617",)),
        n("axial", "Axial compressors", std=("API 617",)),
        n("reciprocating", "Reciprocating compressors", std=("API 618",)),
        n("screw", "Screw compressors", std=("API 619",)),
        n("integrally_geared", "Integrally geared compressors", std=("API 617",)),
        n("air", "Plant & instrument air packages", std=("API 672",), syn=("instrument air compressor",)),
    ),
    n(
        "turbines",
        "Turbines & engines",
        n("gas_turbine", "Gas turbines", std=("API 616",)),
        n("steam_turbine", "Steam turbines", std=("API 611", "API 612")),
        n("gas_engine", "Gas / dual-fuel engines"),
        n("diesel_engine", "Diesel engines"),
    ),
    n(
        "rotating_aux",
        "Rotating equipment auxiliaries",
        n(
            "mechanical_seals",
            "Mechanical seals & seal systems",
            std=("API 682",),
            syn=("seal support system",),
        ),
        n("couplings", "Couplings", std=("API 671",)),
        n("gears", "Gearboxes", std=("API 613", "API 677")),
        n("lube_oil", "Lube & seal oil systems", std=("API 614",)),
        n("bearings", "Bearings"),
    ),
    n("fans_blowers", "Fans & blowers", std=("API 673",)),
    n(
        "heat_transfer",
        "Heat transfer equipment",
        n("shell_tube", "Shell & tube heat exchangers", std=("TEMA", "API 660")),
        n("air_cooled", "Air-cooled heat exchangers", std=("API 661",), syn=("fin fan cooler",)),
        n("plate", "Plate heat exchangers", std=("API 662",), syn=("PHE", "gasketed plate")),
        n("printed_circuit", "Printed circuit / compact heat exchangers", syn=("PCHE",)),
        n("fired_heaters", "Fired heaters & furnaces", std=("API 560",)),
        n("boilers", "Boilers & HRSGs", syn=("heat recovery steam generator",)),
        n("electric_heaters", "Electric process heaters"),
    ),
    n(
        "static",
        "Static equipment",
        n("pressure_vessels", "Pressure vessels", std=("ASME VIII", "PED", "EN 13445"), syn=("drum",)),
        n("columns", "Columns & towers", syn=("distillation column", "absorber")),
        n(
            "separators",
            "Separators & scrubbers",
            syn=("three-phase separator", "knock-out drum", "slug catcher"),
        ),
        n("storage_tanks", "Storage tanks", std=("API 650", "API 620")),
        n("reactors", "Reactors"),
        n("filters", "Filters & strainers", syn=("coalescer", "cartridge filter")),
        n("internals", "Column & vessel internals", syn=("trays", "packing", "demister")),
    ),
    n(
        "valves",
        "Valves",
        n("ball", "Ball valves", std=("API 6D", "API 608", "ISO 17292")),
        n("gate", "Gate valves", std=("API 600", "API 602", "API 6D")),
        n("globe", "Globe valves", std=("API 602", "BS 1873")),
        n("check", "Check valves", std=("API 594", "API 6D")),
        n("butterfly", "Butterfly valves", std=("API 609",)),
        n("plug", "Plug valves", std=("API 599",)),
        n("control", "Control valves", std=("IEC 60534",)),
        n("safety_relief", "Safety & relief valves", std=("API 526", "API 520"), syn=("PSV", "PRV")),
        n("subsea", "Subsea valves", std=("API 6DSS",)),
        n("wellhead", "Wellhead & christmas tree valves", std=("API 6A",)),
        n(
            "actuators",
            "Valve actuators",
            syn=("pneumatic actuator", "electric actuator", "hydraulic actuator"),
        ),
    ),
    n(
        "piping",
        "Piping & fittings",
        n("pipe", "Line pipe & process pipe", std=("API 5L", "ASME B36.10")),
        n("fittings", "Fittings & flanges", std=("ASME B16.5", "ASME B16.9", "ASME B16.47")),
        n("flexible_pipe", "Flexible pipe & hoses", std=("API 17J",), syn=("flexible riser",)),
        n("expansion_joints", "Expansion joints & bellows"),
        n("pipe_supports", "Pipe supports & hangers"),
    ),
    n(
        "electrical",
        "Electrical equipment",
        n("motors", "Electric motors", std=("API 541", "API 547", "IEC 60034"), syn=("induction motor",)),
        n("vfd", "Variable frequency drives", syn=("VSD", "variable speed drive")),
        n("switchgear", "Switchgear & MCC", syn=("motor control centre",)),
        n("transformers", "Transformers"),
        n("generators", "Generators & gensets", syn=("turbo-generator", "diesel generator")),
        n("ups", "UPS & battery systems"),
        n("cables", "Power & instrument cables"),
        n("hazardous_area", "Hazardous-area equipment", std=("ATEX", "IECEx")),
    ),
    n(
        "instrumentation",
        "Instrumentation & control",
        n("flow", "Flow measurement", syn=("flow meter", "metering skid")),
        n("pressure", "Pressure & level instruments"),
        n("temperature", "Temperature instruments"),
        n("analyzers", "Process analyzers", syn=("gas chromatograph", "analyser house")),
        n("dcs", "DCS / control systems", syn=("distributed control system", "PLC")),
        n("sis", "Safety instrumented systems", std=("IEC 61511",), syn=("ESD", "SIS")),
        n("fire_gas", "Fire & gas detection", syn=("F&G",)),
        n("machinery_protection", "Machinery protection & condition monitoring", std=("API 670",)),
    ),
    n(
        "packages",
        "Process packages & skids",
        n("gas_compression", "Gas compression packages"),
        n(
            "water_injection",
            "Water injection & treatment packages",
            syn=("sulphate removal", "SRU", "water injection package", "WI package"),
        ),
        n("chemical_injection", "Chemical injection packages"),
        n("gas_dehydration", "Gas dehydration packages", syn=("TEG unit", "glycol dehydration")),
        n("produced_water", "Produced water treatment", syn=("hydrocyclone", "IGF")),
        n("nitrogen_generation", "Nitrogen generation packages"),
        n("hvac", "HVAC packages"),
        n("power_generation", "Power generation modules"),
    ),
    n(
        "offshore_marine",
        "Offshore & marine systems",
        n(
            "turret_mooring",
            "Turret & mooring systems",
            syn=("internal turret", "external turret", "spread mooring"),
        ),
        n("swivel", "Swivels & fluid transfer systems"),
        n("offloading", "Offloading systems", syn=("tandem offloading", "hawser")),
        n("risers_umbilicals", "Risers & umbilicals", std=("API 17E",)),
        n(
            "subsea_equipment",
            "Subsea production equipment",
            std=("API 17D",),
            syn=("subsea tree", "manifold"),
        ),
        n("cranes", "Offshore cranes", std=("API 2C",)),
        n("lifeboats", "Lifeboats & lifesaving equipment"),
    ),
    n(
        "safety",
        "Safety & fire protection",
        n("fire_fighting", "Fire-fighting systems", syn=("deluge", "foam system")),
        n("gas_detection", "Gas detection"),
        n("passive_fire", "Passive fire protection", syn=("PFP",)),
        n("flare", "Flare systems", std=("API 537",), syn=("flare tip", "flare stack")),
    ),
)

SERVICES: tuple[Node, ...] = (
    n(
        "maintenance",
        "Maintenance",
        n("preventive", "Preventive maintenance"),
        n(
            "condition_based",
            "Condition-based maintenance",
            syn=("vibration analysis", "predictive maintenance"),
        ),
        n("shutdown", "Shutdown & turnaround support", syn=("TAR",)),
    ),
    n(
        "repair",
        "Repair & overhaul",
        n("workshop", "Workshop repair"),
        n("onsite", "On-site repair"),
        n("rewinding", "Motor rewinding"),
    ),
    n(
        "installation",
        "Installation & commissioning",
        n("installation", "Installation"),
        n("commissioning", "Commissioning & start-up"),
    ),
    n("retrofit", "Retrofit, revamp & upgrades", syn=("re-rate", "modernisation")),
    n("spare_parts", "Spare parts supply", syn=("OEM spares", "capital spares")),
    n("field_service", "Field service engineering"),
    n("inspection", "Inspection & NDT", syn=("non-destructive testing", "integrity inspection")),
    n("testing", "Testing & certification", syn=("performance test", "FAT", "SAT")),
    n("engineering", "Engineering & design", syn=("FEED", "detailed engineering")),
    n("training", "Training"),
    n("calibration", "Calibration"),
    n("long_term_service", "Long-term service agreements", syn=("LTSA",)),
)

CERTIFICATIONS: tuple[tuple[str, str, str, str], ...] = (
    # code, name, issuer, category
    ("ISO_9001", "ISO 9001 Quality management", "ISO", "quality"),
    ("ISO_14001", "ISO 14001 Environmental management", "ISO", "environment"),
    ("ISO_45001", "ISO 45001 Occupational health & safety", "ISO", "safety"),
    ("ISO_29001", "ISO/TS 29001 Oil & gas quality management", "ISO", "quality"),
    ("API_Q1", "API Spec Q1", "API", "quality"),
    ("API_Q2", "API Spec Q2", "API", "quality"),
    ("API_6A", "API 6A Monogram", "API", "product"),
    ("API_6D", "API 6D Monogram", "API", "product"),
    ("API_600", "API 600 Monogram", "API", "product"),
    ("API_602", "API 602 Monogram", "API", "product"),
    ("API_610", "API 610 compliance", "API", "product"),
    ("API_17D", "API 17D Monogram", "API", "product"),
    ("ASME_U", "ASME U stamp", "ASME", "product"),
    ("ASME_U2", "ASME U2 stamp", "ASME", "product"),
    ("ASME_S", "ASME S stamp", "ASME", "product"),
    ("PED", "Pressure Equipment Directive (CE)", "EU", "product"),
    ("ATEX", "ATEX", "EU", "hazardous_area"),
    ("IECEX", "IECEx", "IEC", "hazardous_area"),
    ("DNV_TA", "DNV type approval", "DNV", "marine"),
    ("ABS_TA", "ABS type approval", "ABS", "marine"),
    ("BV_TA", "Bureau Veritas type approval", "BV", "marine"),
    ("LR_TA", "Lloyd's Register type approval", "LR", "marine"),
    ("NORSOK", "NORSOK compliance", "Standards Norway", "product"),
    ("SIL", "IEC 61508 SIL capability", "IEC", "safety"),
)

INDUSTRIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("oil_gas_upstream", "Oil & gas - upstream", ("E&P", "exploration and production")),
    ("oil_gas_midstream", "Oil & gas - midstream", ("pipelines",)),
    ("oil_gas_downstream", "Oil & gas - downstream", ("refining",)),
    ("offshore", "Offshore", ()),
    ("fpso", "FPSO & floating production", ("FPSO", "FSO", "FLNG")),
    ("lng", "LNG", ("liquefied natural gas",)),
    ("petrochemical", "Petrochemical & chemical", ()),
    ("power", "Power generation", ()),
    ("renewables", "Renewables & energy transition", ("hydrogen", "CCUS", "offshore wind")),
    ("water", "Water & wastewater", ()),
    ("mining", "Mining & metals", ()),
    ("marine", "Marine & shipbuilding", ()),
    ("manufacturing", "General manufacturing", ()),
    ("epc", "EPC contracting", ("engineering procurement construction",)),
)
