"""The vendor assistant with a scripted AI: it searches the knowledge base, answers only from the records it was
given, drops citations to anything else, carries cited vendors into follow-ups, and meters both AI calls."""

import json
import uuid
from typing import Any

from httpx import AsyncClient

from app.ai.ports import AIError, AIRetryable, Citation, Usage
from app.ai.providers.fake import FakeProvider
from app.core.container import Container
from app.modules.assistant.schemas import AssistantAnswer, AssistantPlan
from app.modules.assistant.service import AssistantService
from app.modules.vendors.schemas import VendorDetail
from tests.conftest import Factory
from tests.test_search import _eq, _vendor

API = "/api/v1"


def test_grounding_drops_unknown_refs() -> None:
    refs: dict[str, VendorDetail] = {"V1": object()}  # type: ignore[dict-item]
    text, removed = AssistantService._ground("Alpha makes BB2 pumps [V1]. Beta too [V7].", refs)
    assert text == "Alpha makes BB2 pumps [V1]. Beta too ." and removed == 1


async def test_assistant_answers_from_the_knowledge_base(
    client: AsyncClient, factory: Factory, container: Container
) -> None:
    tag = uuid.uuid4().hex[:6]
    vid = await _vendor(container, f"Fjord Barrel Pumps {tag}", "NO", [
        _eq("pumps.centrifugal.api610.bb2", "HX-2", model_series="HX-2", standards=["API 610"]),
    ])  # fmt: skip
    seen: dict[str, list[str]] = {"plan": [], "answer": []}

    def plan(text: str) -> dict[str, Any]:
        seen["plan"].append(text)
        if "Do they hold ISO 9001?" in text:  # the follow-up refers back to the cited vendor: no new search
            return {
                "intent": "vendor_details",
                "search_queries": [],
                "vendor_names": [],
                "my_vendors_only": False,
            }
        return {"intent": "find_vendors", "search_queries": [f"BB2 pumps {tag}"], "vendor_names": [],
                "my_vendors_only": False}  # fmt: skip

    def answer(text: str) -> dict[str, Any]:
        seen["answer"].append(text)
        assert f"Fjord Barrel Pumps {tag}" in text and '"ref": "V1"' in text
        return {"answer": f"Fjord Barrel Pumps {tag} makes the HX-2 BB2 pump to API 610 [V1]. "
                          "Acme Pumps also does [V9].\nFrom the web: Nordic Pumps makes BB3 pumps [W1]. "
                          "Made-up Pumps does too [W2].",
                "cited_refs": ["V1", "V9"],
                "web_sources": [{"ref": "W1", "title": "Nordic Pumps BB3", "url": "https://www.nordicpumps-maitest.com/bb3"},
                                {"ref": "W2", "title": "Invented", "url": "https://invented-maitest.org/"}],
                "follow_ups": ["Which of them hold ISO 9001?", "Do you want me to search the web for BB3 pumps?"],
                "actions": [{"kind": "research_vendor", "ref": "V1", "website": "https://www.fjord-maitest.com",
                             "brief": ""},
                            {"kind": "research_vendor", "ref": "V9", "website": "", "brief": ""},
                            {"kind": "discover_vendors", "ref": "", "website": "", "brief": "BB2 pumps in Norway"},
                            {"kind": "send_email", "ref": "V1", "website": "", "brief": ""}]}  # fmt: skip

    fake = FakeProvider(usage=Usage(model="fake-model", input_tokens=2_000, output_tokens=300))
    fake.on(AssistantPlan, plan).on(AssistantAnswer, answer)
    fake.citations = [Citation(url="https://nordicpumps-maitest.com/products", title="Nordic Pumps")]
    previous, container.ai.provider = container.ai.provider, fake
    try:
        tenant = await factory.tenant()
        engineer = await factory.user(tenant, ["engineer"])
        first = await client.post(f"{API}/assistant/chat", headers=engineer.headers,
                                  json={"messages": [{"role": "user", "content": "Who makes BB2 pumps?"}]})  # fmt: skip
        assert first.status_code == 200, first.text
        body = first.json()
        assert "[V1]" in body["answer"] and "[V9]" not in body["answer"] and body["removed_refs"] == 2
        # Web facts cite only pages on sites the search returned; the answer call searched the web.
        assert "[W1]" in body["answer"] and "[W2]" not in body["answer"]
        assert body["web_sources"] == [
            {"ref": "W1", "title": "Nordic Pumps BB3", "url": "https://www.nordicpumps-maitest.com/bb3"}
        ]
        assert [c.web_search for c in fake.calls if c.schema == "AssistantAnswer"] == [True]
        # The workspace: the requirement understood, the shortlist with per-dimension scores and gaps, the steps.
        assert body["requirement"] is not None
        row = body["shortlist"][0]
        assert (row["vendor_id"], row["ref"], row["products"], row["references"]) == (str(vid), "V1", 1, 0)
        assert "No project references on record" in row["flags"] and row["scores"]["track_record"] == 0
        assert [s["key"] for s in body["steps"]] == [
            "understand",
            "search_kb",
            "load_records",
            "answer",
            "check",
            "rank",
        ]
        assert body["steps"][4]["detail"] == "Removed 2 unsupported citations" and body["started_at"]
        assert [v["vendor_id"] for v in body["vendors"]] == [str(vid)] and body["searched"] == [
            f"BB2 pumps {tag}"
        ]
        assert body["follow_ups"] == ["Which of them hold ISO 9001?"]
        # Only actions the app can perform, on vendors in the records: the unknown ref and kind are dropped, and the
        # website is carried because the vendor has none on record.
        assert body["actions"] == [
            {"kind": "research_vendor", "label": f"Research Fjord Barrel Pumps {tag}'s website",
             "vendor_id": str(vid), "vendor_name": f"Fjord Barrel Pumps {tag}",
             "website": "https://www.fjord-maitest.com", "brief": None},
            {"kind": "discover_vendors", "label": "Add new suppliers to the knowledge base", "vendor_id": None,
             "vendor_name": None, "website": None, "brief": "BB2 pumps in Norway"},
        ]  # fmt: skip

        follow = await client.post(f"{API}/assistant/chat", headers=engineer.headers, json={
            "messages": [
                {"role": "user", "content": "Who makes BB2 pumps?"},
                {"role": "assistant", "content": body["answer"]},
                {"role": "user", "content": "Do they hold ISO 9001?"},
            ],
            "context_vendor_ids": [str(vid)],
        })  # fmt: skip
        assert follow.status_code == 200, follow.text
        assert follow.json()["searched"] == []  # answered from the vendor carried over, no new search
        assert len(seen["answer"]) == 2

        usage = (await client.get(f"{API}/ai/usage", headers=engineer.headers)).json()
        prompts = {p["prompt"].split("@")[0]: p["calls"] for p in usage["by_prompt"]}
        assert prompts["assistant_plan"] == 2 and prompts["assistant_answer"] == 2
    finally:
        container.ai.provider = previous

    # The records the model saw are approved facts only, as JSON.
    records = json.loads(
        seen["answer"][0].split("Vendor records:\n", 1)[1].split("<<<DATA\n", 1)[1].split("\nDATA>>>")[0]
    )
    assert records[0]["equipment"] == ["BB2 - Between bearings, radially split, 1-2 stage - HX-2 - API 610"]


async def test_assistant_needs_ai(client: AsyncClient, factory: Factory, container: Container) -> None:
    previous, container.ai.provider = container.ai.provider, None
    try:
        engineer = await factory.user(await factory.tenant(), ["engineer"])
        resp = await client.post(f"{API}/assistant/chat", headers=engineer.headers,
                                 json={"messages": [{"role": "user", "content": "hi"}]})  # fmt: skip
        assert resp.status_code == 503
    finally:
        container.ai.provider = previous


async def test_assistant_saves_chats(client: AsyncClient, factory: Factory, container: Container) -> None:
    """Each question and answer goes into the user's saved chat; chats can be listed, reopened, renamed and deleted,
    and nobody else (not even a colleague) can open or add to them."""
    calls = {"answer": 0}

    def answer(text: str) -> dict[str, Any]:
        calls["answer"] += 1
        return {"answer": f"Answer {calls['answer']}", "cited_refs": [], "web_sources": [], "follow_ups": ["And in Norway?"],
                "actions": []}  # fmt: skip

    fake = FakeProvider(usage=Usage(model="fake-model", input_tokens=100, output_tokens=20))
    fake.on(AssistantPlan, lambda _: {"intent": "knowledge_question", "search_queries": [], "vendor_names": [],
                                      "my_vendors_only": False})  # fmt: skip
    fake.on(AssistantAnswer, answer)
    previous, container.ai.provider = container.ai.provider, fake
    try:
        tenant = await factory.tenant()
        engineer = await factory.user(tenant, ["engineer"])
        colleague = await factory.user(tenant, ["engineer"])
        question = (
            "Which vendors supply   subsea christmas trees, wellheads and manifolds for deepwater fields?"
        )
        first = await client.post(f"{API}/assistant/chat", headers=engineer.headers,
                                  json={"messages": [{"role": "user", "content": question}]})  # fmt: skip
        assert first.status_code == 200, first.text
        chat_id = first.json()["conversation_id"]
        assert chat_id
        second = await client.post(f"{API}/assistant/chat", headers=engineer.headers, json={
            "messages": [{"role": "user", "content": question}, {"role": "assistant", "content": "Answer 1"},
                         {"role": "user", "content": "And in Norway?"}],
            "conversation_id": chat_id,
        })  # fmt: skip
        assert second.status_code == 200 and second.json()["conversation_id"] == chat_id

        listed = (await client.get(f"{API}/assistant/conversations", headers=engineer.headers)).json()
        assert [c["id"] for c in listed] == [chat_id]
        assert (
            listed[0]["title"] == "Which vendors supply subsea christmas trees, wellheads and manifolds for…"
        )
        detail = (
            await client.get(f"{API}/assistant/conversations/{chat_id}", headers=engineer.headers)
        ).json()
        assert [(m["role"], m["content"]) for m in detail["messages"]] == [
            ("user", question), ("assistant", "Answer 1"), ("user", "And in Norway?"), ("assistant", "Answer 2"),
        ]  # fmt: skip
        assert detail["messages"][0]["reply"] is None
        assert detail["messages"][3]["reply"]["follow_ups"] == ["And in Norway?"]

        # A colleague can't see, open, add to, rename or delete it; adding fails before any AI call.
        assert (await client.get(f"{API}/assistant/conversations", headers=colleague.headers)).json() == []
        url = f"{API}/assistant/conversations/{chat_id}"
        assert (await client.get(url, headers=colleague.headers)).status_code == 404
        assert (await client.patch(url, headers=colleague.headers, json={"title": "x"})).status_code == 404
        assert (await client.delete(url, headers=colleague.headers)).status_code == 404
        sneak = await client.post(f"{API}/assistant/chat", headers=colleague.headers, json={
            "messages": [{"role": "user", "content": "hi"}], "conversation_id": chat_id,
        })  # fmt: skip
        assert sneak.status_code == 404 and calls["answer"] == 2

        renamed = await client.patch(url, headers=engineer.headers, json={"title": "  Subsea   trees "})
        assert renamed.status_code == 200 and renamed.json()["title"] == "Subsea trees"
        assert (await client.delete(url, headers=engineer.headers)).status_code == 204
        assert (await client.get(url, headers=engineer.headers)).status_code == 404
        assert (await client.get(f"{API}/assistant/conversations", headers=engineer.headers)).json() == []
    finally:
        container.ai.provider = previous


async def test_ai_failures_are_explained(client: AsyncClient, factory: Factory, container: Container) -> None:
    """An AI timeout (after retries) or unusable answer comes back as a clear 502, not a bare 500."""

    def timeout(_: str) -> dict[str, Any]:
        raise AIRetryable("Request timed out")

    def unusable(_: str) -> dict[str, Any]:
        raise AIError("The answer didn't match the schema")

    engineer = await factory.user(await factory.tenant(), ["engineer"])
    previous, previous_sleep = container.ai.provider, container.ai._sleep

    async def no_sleep(_: float) -> None:
        return None

    container.ai._sleep = no_sleep
    try:
        for handler, status, code in ((timeout, 502, "ai_failed"), (unusable, 502, "ai_failed")):
            container.ai.provider = FakeProvider().on(AssistantPlan, handler)
            resp = await client.post(f"{API}/assistant/chat", headers=engineer.headers,
                                     json={"messages": [{"role": "user", "content": "BB2 pumps"}]})  # fmt: skip
            assert resp.status_code == status and resp.json()["code"] == code, resp.text
    finally:
        container.ai.provider, container.ai._sleep = previous, previous_sleep
