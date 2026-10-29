"""The vendor assistant with a scripted AI: it searches the knowledge base, answers only from the records it was
given, drops citations to anything else, carries cited vendors into follow-ups, and meters both AI calls."""

import json
import uuid
from typing import Any

from httpx import AsyncClient

from app.ai.ports import Usage
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
                          "Acme Pumps also does [V9].",
                "cited_refs": ["V1", "V9"], "follow_ups": ["Which of them hold ISO 9001?"],
                "actions": [{"kind": "research_vendor", "ref": "V1", "website": "https://www.fjord-maitest.com",
                             "brief": ""},
                            {"kind": "research_vendor", "ref": "V9", "website": "", "brief": ""},
                            {"kind": "discover_vendors", "ref": "", "website": "", "brief": "BB2 pumps in Norway"},
                            {"kind": "send_email", "ref": "V1", "website": "", "brief": ""}]}  # fmt: skip

    fake = FakeProvider(usage=Usage(model="fake-model", input_tokens=2_000, output_tokens=300))
    fake.on(AssistantPlan, plan).on(AssistantAnswer, answer)
    previous, container.ai.provider = container.ai.provider, fake
    try:
        tenant = await factory.tenant()
        engineer = await factory.user(tenant, ["engineer"])
        first = await client.post(f"{API}/assistant/chat", headers=engineer.headers,
                                  json={"messages": [{"role": "user", "content": "Who makes BB2 pumps?"}]})  # fmt: skip
        assert first.status_code == 200, first.text
        body = first.json()
        assert "[V1]" in body["answer"] and "[V9]" not in body["answer"] and body["removed_refs"] == 1
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
            {"kind": "discover_vendors", "label": "Discover vendors on the web", "vendor_id": None,
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
