"""RFQ by e-mail: a preview, one message per vendor with the requirements and due date, replies to the buyer, vendors
without an address skipped, a draft issued by the send, and the send remembered on each vendor row."""

import uuid

from httpx import AsyncClient

from app.core.container import Container
from app.domain.enums import ProposalItemAction as A
from app.modules.proposals.service import NewItem
from tests.conftest import Factory, RecordingEmailSender
from tests.test_search import _vendor

API = "/api/v1"


async def test_rfq_email(client: AsyncClient, factory: Factory, container: Container) -> None:
    tag = uuid.uuid4().hex[:6]
    mailed = await _vendor(container, f"Mail Pumps {tag}", "NO", [
        NewItem(A.ADD_CONTACT, {"contact_type": "sales", "email": f"sales@mailpumps-{tag}-maitest.com"}),
    ])  # fmt: skip
    silent = await _vendor(container, f"Quiet Pumps {tag}", "DE", [])
    buyer = await factory.user(await factory.tenant(), ["tenant_admin"])
    outbox: RecordingEmailSender = container.email  # type: ignore[assignment]

    rfq = (await client.post(f"{API}/rfqs", headers=buyer.headers, json={
        "title": f"Seawater pumps {tag}", "due_date": "2027-01-15", "description": "Two pumps, 2 x 100%.",
        "requirements": [{"text": "API 610 type VS4", "mandatory": True}, {"text": "Spares for 2 years", "mandatory": False}],
    })).json()  # fmt: skip
    for v in (mailed, silent):
        await client.post(
            f"{API}/rfqs/{rfq['id']}/vendors", headers=buyer.headers, json={"vendor_id": str(v)}
        )

    preview = (await client.get(f"{API}/rfqs/{rfq['id']}/email", headers=buyer.headers)).json()
    assert preview["subject"].startswith(f"Request for quotation {rfq['rfq_number']}: Seawater pumps {tag}")
    assert "API 610 type VS4" in preview["body"] and "Spares for 2 years (optional)" in preview["body"]
    assert "15 January 2027" in preview["body"] and preview["reply_to"] == buyer.email
    emails = {r["vendor_id"]: r["email"] for r in preview["recipients"]}
    assert emails == {str(mailed): f"sales@mailpumps-{tag}-maitest.com", str(silent): None}

    # A test copy goes only to the buyer and changes nothing.
    sent_before = len(outbox.sent)
    test = (await client.post(f"{API}/rfqs/{rfq['id']}/email", headers=buyer.headers,
                              json={"vendor_ids": [str(mailed)], "test_to_me": True})).json()  # fmt: skip
    assert test["sent"] == [buyer.email] and outbox.sent[-1].subject.startswith("[TEST]")
    assert len(outbox.sent) == sent_before + 1
    assert (await client.get(f"{API}/rfqs/{rfq['id']}", headers=buyer.headers)).json()["status"] == "draft"

    # The real send: one message per vendor with an address; the draft is issued and the send remembered.
    done = (await client.post(f"{API}/rfqs/{rfq['id']}/email", headers=buyer.headers, json={
        "vendor_ids": [str(mailed), str(silent)], "note": "Please quote by mid-January.",
    })).json()  # fmt: skip
    assert done["sent"] == [f"sales@mailpumps-{tag}-maitest.com"] and done["issued"] is True
    assert len(done["skipped"]) == 1 and "no e-mail address" in done["skipped"][0]
    message = outbox.last_to(f"sales@mailpumps-{tag}-maitest.com")
    assert message.reply_to == buyer.email and f"Dear Mail Pumps {tag}," in message.body
    assert "Please quote by mid-January." in message.body and "API 610 type VS4" in message.body
    assert message.html and "<li>API 610 type VS4</li>" in message.html
    rows = {
        r["vendor"]["id"]: r
        for r in (await client.get(f"{API}/rfqs/{rfq['id']}/vendors", headers=buyer.headers)).json()
    }
    assert rows[str(mailed)]["status"] == "invited" and rows[str(mailed)]["emailed_to"].startswith("sales@")
    assert rows[str(mailed)]["emailed_at"] and rows[str(silent)]["emailed_at"] is None
    assert (await client.get(f"{API}/rfqs/{rfq['id']}", headers=buyer.headers)).json()["status"] == "issued"

    # Viewers can't send.
    viewer = await factory.user(buyer.tenant_id, ["viewer"])
    denied = await client.post(
        f"{API}/rfqs/{rfq['id']}/email", headers=viewer.headers, json={"vendor_ids": [str(mailed)]}
    )
    assert denied.status_code == 403

    # Platform staff viewing as the organization: their own address (outside its data) still gets the replies.
    admin = await factory.user(None, platform_roles=["super_admin"])
    viewing = {**admin.headers, "X-Act-As-Tenant": str(buyer.tenant_id)}
    staff = (await client.get(f"{API}/rfqs/{rfq['id']}/email", headers=viewing)).json()
    assert staff["reply_to"] == admin.email
    copy = (await client.post(f"{API}/rfqs/{rfq['id']}/email", headers=viewing,
                              json={"vendor_ids": [str(mailed)], "test_to_me": True})).json()  # fmt: skip
    assert copy["sent"] == [admin.email]

    # Testing setting: a vendor with no address gets the RFQ at the test mailbox, marked as a test.
    container.settings.rfq_test_recipient = "rfq-test@maitest.com"
    try:
        shown = (await client.get(f"{API}/rfqs/{rfq['id']}/email", headers=buyer.headers)).json()
        quiet = next(r for r in shown["recipients"] if r["vendor_id"] == str(silent))
        assert quiet["email"] == "rfq-test@maitest.com" and quiet["test_address"] is True
        res = (await client.post(f"{API}/rfqs/{rfq['id']}/email", headers=buyer.headers,
                                 json={"vendor_ids": [str(silent)]})).json()  # fmt: skip
        assert res["sent"] == ["rfq-test@maitest.com"] and not res["skipped"]
        assert outbox.last_to("rfq-test@maitest.com").subject.startswith(f"[TEST for Quiet Pumps {tag}]")
    finally:
        container.settings.rfq_test_recipient = None
