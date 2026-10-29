"""Knowledge base read APIs and the tenant overlay ("My vendors")."""

import uuid

from httpx import AsyncClient
from sqlalchemy import select

from app.core.container import Container
from app.db.session import open_session
from app.domain.normalization import normalize_name
from app.domain.principal import TenantContext
from app.modules.taxonomy.models import CertificationType, EquipmentCategory, Industry
from app.modules.vendors.models import (
    Vendor,
    VendorCertification,
    VendorDomain,
    VendorEquipment,
    VendorIndustry,
)
from tests.conftest import Factory


async def make_vendor(
    container: Container,
    name: str,
    *,
    equipment: list[str] = (),  # type: ignore[assignment]
    certifications: list[str] = (),  # type: ignore[assignment]
    industries: list[str] = (),  # type: ignore[assignment]
    country: str = "US",
    domain: str | None = None,
) -> uuid.UUID:
    """Inserts an approved vendor directly (the proposal workflow that normally does this arrives in slice 2)."""
    async with open_session(container.sessionmaker, TenantContext.platform()) as s:
        v = Vendor(
            legal_name=f"{name} Inc.",
            display_name=name,
            normalized_name=normalize_name(name),
            hq_country=country,
            description=f"{name} makes industrial equipment.",
        )
        s.add(v)
        await s.flush()
        s.add(
            VendorDomain(
                vendor_id=v.id, domain=domain or f"{uuid.uuid4().hex[:8]}.example.com", is_primary=True
            )
        )
        for code in equipment:
            cat = await s.scalar(select(EquipmentCategory).where(EquipmentCategory.code == code))
            assert cat is not None, code
            s.add(VendorEquipment(vendor_id=v.id, category_id=cat.id, name=f"{cat.name} by {name}"))
        for code in certifications:
            ct = await s.scalar(select(CertificationType).where(CertificationType.code == code))
            assert ct is not None, code
            s.add(VendorCertification(vendor_id=v.id, certification_type_id=ct.id))
        for code in industries:
            ind = await s.scalar(select(Industry).where(Industry.code == code))
            assert ind is not None, code
            s.add(VendorIndustry(vendor_id=v.id, industry_id=ind.id))
        await s.commit()
        return v.id


async def test_taxonomy_endpoints(client: AsyncClient, factory: Factory) -> None:
    viewer = await factory.user(await factory.tenant(), ["viewer"])
    eq = (await client.get("/api/v1/taxonomy/equipment-categories", headers=viewer.headers)).json()
    codes = {c["code"]: c for c in eq}
    bb2 = codes["pumps.centrifugal.api610.bb2"]
    assert bb2["level"] == 3 and "API 610" in bb2["standards"]
    assert codes["pumps.centrifugal.api610"]["id"] == bb2["parent_id"]
    certs = (await client.get("/api/v1/taxonomy/certification-types", headers=viewer.headers)).json()
    assert any(c["code"] == "API_Q1" for c in certs)
    assert (await client.get("/api/v1/taxonomy/industries", headers=viewer.headers)).status_code == 200


async def test_search_filters(client: AsyncClient, factory: Factory, container: Container) -> None:
    tag = uuid.uuid4().hex[:6]
    pump_co = await make_vendor(
        container,
        f"Zeta Pumps {tag}",
        equipment=["pumps.centrifugal.api610.bb2"],
        certifications=["API_Q1"],
        industries=["fpso"],
        country="NO",
    )
    valve_co = await make_vendor(container, f"Zeta Valves {tag}", equipment=["valves.ball"], country="IT")
    viewer = await factory.user(await factory.tenant(), ["viewer"])

    async def ids(**params: str) -> set[str]:
        resp = await client.get(
            "/api/v1/vendors", params={"q": f"zeta {tag}", **params}, headers=viewer.headers
        )
        assert resp.status_code == 200, resp.text
        return {v["id"] for v in resp.json()["items"]}

    assert await ids() == {str(pump_co), str(valve_co)}
    # A parent category matches vendors of every descendant category.
    assert await ids(equipment_category="pumps") == {str(pump_co)}
    assert await ids(equipment_category="pumps.centrifugal.api610.bb2") == {str(pump_co)}
    assert await ids(equipment_category="pumps.centrifugal.api610.oh2") == set()
    assert await ids(certification="API_Q1") == {str(pump_co)}
    assert await ids(industry="fpso") == {str(pump_co)}
    assert await ids(country="Italy") == {str(valve_co)}

    bad = await client.get("/api/v1/vendors", params={"country": "Atlantis"}, headers=viewer.headers)
    assert bad.status_code == 422


async def test_search_by_alias_free_text_and_domain(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    vid = await make_vendor(container, f"Orbital Compression {tag} GmbH", domain=f"orbital-{tag}.example.com")
    viewer = await factory.user(await factory.tenant(), ["viewer"])
    for q in (f"orbital compression {tag}", f"Orbital Compression {tag} AG", f"orbital-{tag}.example"):
        items = (await client.get("/api/v1/vendors", params={"q": q}, headers=viewer.headers)).json()["items"]
        assert str(vid) in {v["id"] for v in items}, q


async def test_pagination_is_alphabetical_and_complete(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    names = [f"Pagina {tag} {letter}" for letter in "DACEB"]
    for name in names:
        await make_vendor(container, name)
    viewer = await factory.user(await factory.tenant(), ["viewer"])
    seen: list[str] = []
    cursor = None
    while True:
        params = {"q": f"pagina {tag}", "limit": "2", **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/api/v1/vendors", params=params, headers=viewer.headers)).json()
        seen += [v["display_name"] for v in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == sorted(names)


async def test_vendor_detail(client: AsyncClient, factory: Factory, container: Container) -> None:
    vid = await make_vendor(
        container,
        f"Detail Pumps {uuid.uuid4().hex[:6]}",
        equipment=["pumps.centrifugal.api610.oh2"],
        certifications=["ISO_9001"],
        industries=["lng"],
    )
    viewer = await factory.user(await factory.tenant(), ["viewer"])
    d = (await client.get(f"/api/v1/vendors/{vid}", headers=viewer.headers)).json()
    assert (
        d["equipment_count"] == 1 and d["equipment"][0]["category"]["code"] == "pumps.centrifugal.api610.oh2"
    )
    assert d["certifications"][0]["certification"]["code"] == "ISO_9001"
    assert [i["code"] for i in d["industries"]] == ["lng"]
    assert len(d["domains"]) == 1 and d["tenant"] is None
    assert (await client.get(f"/api/v1/vendors/{uuid.uuid4()}", headers=viewer.headers)).status_code == 404


async def test_tenant_overlay_is_private(client: AsyncClient, factory: Factory, container: Container) -> None:
    vid = await make_vendor(container, f"Overlay Co {uuid.uuid4().hex[:6]}")
    tenant_a, tenant_b = await factory.tenant(), await factory.tenant()
    manager_a = await factory.user(tenant_a, ["manager"])
    viewer_a = await factory.user(tenant_a, ["viewer"])
    manager_b = await factory.user(tenant_b, ["manager"])

    body = {
        "avl_status": "approved",
        "internal_vendor_code": "V-001",
        "tags": ["rotating", " rotating ", "pumps"],
    }
    put = await client.put(f"/api/v1/my-vendors/{vid}", json=body, headers=manager_a.headers)
    assert put.status_code == 200, put.text
    assert put.json()["tags"] == ["pumps", "rotating"]

    # Viewers can read but not change the organization's approval.
    assert (
        await client.put(f"/api/v1/my-vendors/{vid}", json=body, headers=viewer_a.headers)
    ).status_code == 403
    seen_by_a = (await client.get(f"/api/v1/vendors/{vid}", headers=viewer_a.headers)).json()["tenant"]
    assert seen_by_a["avl_status"] == "approved" and seen_by_a["internal_vendor_code"] == "V-001"

    # Tenant B sees the same global vendor without A's private data, and its filter doesn't match A's status.
    assert (await client.get(f"/api/v1/vendors/{vid}", headers=manager_b.headers)).json()["tenant"] is None
    approved_b = await client.get(
        "/api/v1/vendors", params={"avl_status": "approved"}, headers=manager_b.headers
    )
    assert str(vid) not in {v["id"] for v in approved_b.json()["items"]}
    approved_a = await client.get(
        "/api/v1/vendors", params={"avl_status": "approved"}, headers=manager_a.headers
    )
    assert str(vid) in {v["id"] for v in approved_a.json()["items"]}
    mine_a = await client.get("/api/v1/vendors", params={"mine": "true"}, headers=manager_a.headers)
    mine_b = await client.get("/api/v1/vendors", params={"mine": "true"}, headers=manager_b.headers)
    assert [v["id"] for v in mine_a.json()["items"]] == [str(vid)] and mine_b.json()["items"] == []

    # Changes are audited in A's log only.
    logs = (
        await client.get("/api/v1/audit-logs", params={"action": "tenant_vendor."}, headers=manager_a.headers)
    ).json()
    assert any(e["entity_id"] == str(vid) for e in logs["items"])

    assert (await client.delete(f"/api/v1/my-vendors/{vid}", headers=manager_a.headers)).status_code == 204
    assert (await client.get(f"/api/v1/vendors/{vid}", headers=viewer_a.headers)).json()["tenant"] is None


async def test_platform_admin_without_tenant_cannot_set_overlay(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    vid = await make_vendor(container, f"Platform Co {uuid.uuid4().hex[:6]}")
    admin = await factory.user(None, platform_roles=["super_admin"])
    resp = await client.put(
        f"/api/v1/my-vendors/{vid}", json={"avl_status": "approved"}, headers=admin.headers
    )
    assert resp.status_code == 403 and resp.json()["code"] == "tenant_required"
