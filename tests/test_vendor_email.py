"""Vendor lists carry each vendor's main email (the RFQ vendor table, its suggestions and the vendor search show it):
an approved company contact, the sales address before the general one."""

import uuid

from httpx import AsyncClient

from app.core.container import Container
from app.domain.enums import ProposalItemAction as A
from app.modules.proposals.service import NewItem
from tests.conftest import Factory
from tests.test_search import _vendor

API = "/api/v1"


async def test_vendor_lists_show_the_main_email(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    vid = await _vendor(container, f"Mailbox Pumps {tag}", "NO", [
        NewItem(A.ADD_CONTACT, {"contact_type": "general", "label": "Head office", "email": f"info@mailbox-{tag}-maitest.com"}),
        NewItem(A.ADD_CONTACT, {"contact_type": "sales", "label": "Sales", "email": f"sales@mailbox-{tag}-maitest.com"}),
    ])  # fmt: skip
    silent = await _vendor(container, f"Silent Valves {tag}", "NO", [])
    user = await factory.user(await factory.tenant(), ["tenant_admin"])

    page = (await client.get(f"{API}/vendors", headers=user.headers, params={"q": tag, "limit": 10})).json()
    emails = {v["id"]: v["email"] for v in page["items"]}
    assert emails[str(vid)] == f"sales@mailbox-{tag}-maitest.com"  # sales before general
    assert emails[str(silent)] is None

    rfq = (await client.post(f"{API}/rfqs", headers=user.headers, json={"title": f"Pumps {tag}"})).json()
    rows = (
        await client.post(
            f"{API}/rfqs/{rfq['id']}/vendors", headers=user.headers, json={"vendor_id": str(vid)}
        )
    ).json()
    assert rows[0]["vendor"]["email"] == f"sales@mailbox-{tag}-maitest.com"
