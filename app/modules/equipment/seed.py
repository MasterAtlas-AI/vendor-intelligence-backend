"""Shared default equipment attributes (SOW 5.2), synced by `app.cli bootstrap` with the taxonomy.

Organizations add their own on top (Settings > Equipment attributes). Keys are permanent: stored values use them.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.equipment.models import EquipmentAttribute
from app.modules.equipment.units import canonical_unit

# key, label, quantity ("" = text), synonyms (column headers and spec labels that mean this attribute)
DEFAULTS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "capacity",
        "Capacity / flow",
        "flow",
        ("capacity", "flow", "flow rate", "rated flow", "capacities", "max flow", "design flow"),
    ),
    ("head", "Head", "head", ("head", "differential head", "rated head", "max head", "heads", "tdh")),
    (
        "design_pressure",
        "Design pressure",
        "pressure",
        (
            "design pressure",
            "design press",
            "pressure",
            "max working pressure",
            "working pressure",
            "pressures",
            "dp",
        ),
    ),
    (
        "design_temperature",
        "Design temperature",
        "temperature",
        ("design temperature", "design temp", "temperature", "max fluid temperature", "temperatures", "dt"),
    ),
    (
        "power",
        "Power",
        "power",
        (
            "power",
            "rated power",
            "motor power",
            "absorbed power",
            "electrical absorbed power",
            "motor kw",
            "kw",
            "rating kw",
        ),
    ),
    (
        "weight",
        "Weight",
        "mass",
        ("weight", "dry weight", "dry wt", "operating weight", "mass", "package weight"),
    ),
    ("footprint", "Footprint", "area", ("footprint", "plot area", "area")),
    ("voltage", "Voltage", "voltage", ("voltage", "supply voltage", "rated voltage")),
    ("volume", "Volume", "volume", ("volume", "capacity volume", "storage volume")),
    (
        "dimensions",
        "Dimensions (L x W x H)",
        "",
        ("dimensions", "dimension", "size", "l x w x h", "overall dimensions"),
    ),
    (
        "material",
        "Material of construction",
        "",
        ("material", "moc", "materials", "material of construction"),
    ),
    ("pressure_rating", "Pressure rating / class", "", ("rating", "class", "pressure class", "ansi class")),
    ("utilities", "Utility requirements", "", ("utilities", "utility", "utility requirements")),
    # SOW 5.32 / 5.33: technical features, interfaces, package configuration, maintenance and spares
    (
        "technical_features",
        "Technical features / special design",
        "",
        ("technical features", "special features", "design features", "special design"),
    ),
    ("interfaces", "Interfaces", "", ("interfaces", "interface", "terminal points", "tie-ins")),
    (
        "package_configuration",
        "Package configuration",
        "",
        ("package configuration", "skid configuration", "package scope", "scope of supply"),
    ),
    (
        "maintenance_requirements",
        "Maintenance requirements",
        "",
        ("maintenance requirements", "maintenance", "maintenance interval", "overhaul interval"),
    ),
    (
        "spares_philosophy",
        "Spares philosophy",
        "",
        ("spares philosophy", "spare philosophy", "spares", "spare parts", "sparing"),
    ),
    ("class_society", "Classification society", "", ("class society", "classification", "class approval")),
    (
        "performance_guarantee",
        "Performance guarantee",
        "",
        ("performance guarantee", "guarantee", "guarantees"),
    ),
    (
        "hazardous_area",
        "Hazardous area / zone",
        "",
        ("area classification", "hazardous area", "zone", "atex zone"),
    ),
)


async def sync_attributes(session: AsyncSession) -> int:
    """Upserts the shared defaults (tenant_id NULL, all categories). Must run in a platform session."""
    existing = {
        a.key: a
        for a in await session.scalars(
            select(EquipmentAttribute).where(
                EquipmentAttribute.tenant_id.is_(None), EquipmentAttribute.category_id.is_(None)
            )
        )
    }
    for order, (key, label, quantity, synonyms) in enumerate(DEFAULTS):
        row = existing.get(key)
        if row is None:
            row = EquipmentAttribute(tenant_id=None, category_id=None, key=key)
            session.add(row)
        row.label = label
        row.data_type = "number" if quantity else "text"
        row.quantity = quantity
        row.unit = canonical_unit(quantity)
        row.synonyms = list(synonyms)
        row.sort_order = order
    await session.flush()
    return len(DEFAULTS)
