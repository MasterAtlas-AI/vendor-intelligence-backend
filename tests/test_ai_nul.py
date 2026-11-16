"""AI answers with a NUL character (a garbled accent, "R\\x00e9sidence") are cleaned before anything stores them:
Postgres refuses NUL in text and jsonb, which once stopped a whole-knowledge-base run."""

from typing import Any

from app.ai.providers.fake import FakeProvider
from app.ai.service import CallContext
from app.core.container import Container
from app.core.text import strip_nul
from app.modules.quality.lookup import FIND_COUNTRY, CountryFinding


def test_strip_nul_cleans_nested_values() -> None:
    assert strip_nul({"a": "R\x00e9s", "b": ["x\x00", ("y\x00",)], "n": 3}) == {
        "a": "Re9s",
        "b": ["x", ("y",)],
        "n": 3,
    }


async def test_ai_answers_lose_nul_characters(container: Container) -> None:
    def answer(_: str) -> dict[str, Any]:
        return {"country": "DZ", "city": "Bir Mourad R\x00e9s", "source_url": None, "confidence": 0.9,
                "reason": "The office is on Rue Larbi Saada, Bir Mourad R\x00e9s."}  # fmt: skip

    previous, container.ai.provider = container.ai.provider, FakeProvider().on(CountryFinding, answer)
    try:
        res = await container.ai.run(
            FIND_COUNTRY, CountryFinding, CallContext(None), company={"name": "Test Co"}
        )
    finally:
        container.ai.provider = previous
    assert res.output.city == "Bir Mourad Re9s"
    assert "\x00" not in res.output.reason and res.output.country == "DZ"
