"""Equipment technology trends by project year (SOW 23.6): ratings, design choices, standards and makers."""

import uuid

from httpx import AsyncClient

from tests.conftest import Factory

API = "/api/v1"
BB2 = "pumps.centrifugal.api610.bb2"


async def test_technology_trends(client: AsyncClient, factory: Factory) -> None:
    tag = uuid.uuid4().hex[:6]
    tenant = await factory.tenant()
    admin = await factory.user(tenant, ["tenant_admin"])
    viewer = await factory.user(tenant, ["viewer"])

    async def project(year: int | None, lines: list[tuple[float, str, str]]) -> None:
        r = await client.post(f"{API}/projects", headers=admin.headers,
                              json={"name": f"FPSO {year} {tag}", "kind": "historical", "year": year})  # fmt: skip
        assert r.status_code == 201, r.text
        for i, (power, material, maker) in enumerate(lines):
            line = await client.post(f"{API}/equipment", headers=admin.headers, json={
                "project_id": r.json()["id"], "name": "Seawater lift pump", "tag": f"P-{year}-{i}",
                "category_code": BB2, "vendor_name": maker, "standards": ["API 610"],
                "attributes": {"power": power, "material": material},
            })  # fmt: skip
            assert line.status_code == 201, line.text

    await project(2014, [(50, "Carbon steel", "Old Pumps"), (60, "carbon steel", "Old Pumps")])
    await project(2023, [(100, "Super duplex", "New Pumps"), (110, "Super duplex", "Old Pumps")])
    await project(None, [(80, "Duplex", "Old Pumps")])

    r = await client.get(f"{API}/equipment/trends", headers=viewer.headers, params={"category_code": "pumps"})
    assert r.status_code == 200, r.text
    t = r.json()
    assert [(y["year"], y["lines"], y["projects"]) for y in t["years"]] == [(2014, 2, 1), (2023, 2, 1)]
    assert t["undated_lines"] == 1
    # The organization's categories with dated lines; without a category, the largest one is shown.
    assert [(c["code"], c["lines"]) for c in t["categories"]] == [(BB2, 4)]
    default = (await client.get(f"{API}/equipment/trends", headers=viewer.headers)).json()
    assert default["category_code"] == BB2 and len(default["years"]) == 2
    power = next(n for n in t["numeric"] if n["key"] == "power")
    assert [(p["year"], p["median"]) for p in power["points"]] == [(2014, 55), (2023, 105)]
    assert round(power["change"], 2) == 0.91
    material = next(s for s in t["shares"] if s["key"] == "material")
    duplex = next(row for row in material["rows"] if row["value"] == "Super duplex")
    assert duplex["by_year"] == {"2014": 0, "2023": 1} and duplex["first_year"] == 2023
    makers = next(s for s in t["shares"] if s["key"] == "maker")
    assert {row["value"] for row in makers["rows"]} == {"Old Pumps", "New Pumps"}
    assert any(h.startswith("Median power rose 91%") for h in t["highlights"])
    assert any("Super duplex first appears in 2023" in h for h in t["highlights"])

    # Another organization's history is not part of it; an unknown category is a 404.
    outsider = await factory.user(await factory.tenant(), ["tenant_admin"])
    other = (
        await client.get(f"{API}/equipment/trends", headers=outsider.headers, params={"category_code": BB2})
    ).json()
    assert other["years"] == [] and other["numeric"] == []
    missing = await client.get(
        f"{API}/equipment/trends", headers=viewer.headers, params={"category_code": "nope"}
    )
    assert missing.status_code == 404
