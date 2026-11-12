"""Idempotent sync of the seed taxonomy into the database (run by `app.cli bootstrap`).

Upserts by `code`; never deletes (facts reference categories). Removing a node from seed_data leaves it in place.
"""

from collections.abc import Iterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.equipment.seed import sync_attributes
from app.modules.taxonomy.models import CertificationType, EquipmentCategory, Industry, ServiceCategory
from app.modules.taxonomy.seed_data import CERTIFICATIONS, EQUIPMENT, INDUSTRIES, SERVICES, Node


def flatten(nodes: tuple[Node, ...], prefix: str = "") -> Iterator[tuple[str, str | None, int, Node]]:
    """Yields (code, parent_code, sort_order, node) parents-first."""
    for order, node in enumerate(nodes):
        code = f"{prefix}.{node.key}" if prefix else node.key
        yield code, prefix or None, order, node
        yield from flatten(node.children, code)


async def _sync_tree(session: AsyncSession, model: Any, nodes: tuple[Node, ...]) -> int:
    existing = {c.code: c for c in await session.scalars(select(model))}
    count = 0
    for code, parent_code, order, node in flatten(nodes):
        row = existing.get(code)
        if row is None:
            row = model(code=code)
            session.add(row)
            existing[code] = row
        row.name = node.name
        row.description = node.description
        row.synonyms = list(node.synonyms)
        row.level = code.count(".")
        row.sort_order = order
        row.parent_id = existing[parent_code].id if parent_code else None
        if hasattr(row, "standards"):
            row.standards = list(node.standards)
        await session.flush()  # parent ids must exist before children reference them
        count += 1
    return count


async def sync_taxonomy(session: AsyncSession) -> dict[str, int]:
    """Caller commits. Global tables, so any session context works."""
    counts = {
        "equipment_categories": await _sync_tree(session, EquipmentCategory, EQUIPMENT),
        "service_categories": await _sync_tree(session, ServiceCategory, SERVICES),
    }

    certs = {c.code: c for c in await session.scalars(select(CertificationType))}
    for code, name, issuer, category in CERTIFICATIONS:
        cert = certs.get(code) or CertificationType(code=code)
        cert.name, cert.issuer, cert.category = name, issuer, category
        session.add(cert)
    counts["certification_types"] = len(CERTIFICATIONS)

    industries = {i.code: i for i in await session.scalars(select(Industry))}
    for code, name, synonyms in INDUSTRIES:
        industry = industries.get(code) or Industry(code=code)
        industry.name, industry.synonyms = name, list(synonyms)
        session.add(industry)
    counts["industries"] = len(INDUSTRIES)

    await session.flush()
    counts["equipment_attributes"] = await sync_attributes(session)
    return counts
