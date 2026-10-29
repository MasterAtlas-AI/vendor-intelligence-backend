"""A scripted provider for tests and local development without an API key. Costs nothing, never touches the network.

Register a handler per output schema; it receives the rendered input and returns the schema instance (or a dict that
validates into it, or an exception to raise).
"""

import hashlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from app.ai.ports import AIError, Citation, EmbeddingResult, LLMResult, Usage

T = TypeVar("T", bound=BaseModel)

Handler = Callable[[str], Any]


@dataclass
class FakeCall:
    schema: str
    instructions: str
    input: str
    web_search: bool
    allowed_domains: list[str] | None


@dataclass
class FakeProvider:
    handlers: dict[type[BaseModel], Handler] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)
    usage: Usage = field(
        default_factory=lambda: Usage(model="fake-model", input_tokens=1000, output_tokens=200)
    )
    calls: list[FakeCall] = field(default_factory=list)
    name: str = "fake"
    model: str = "fake-model"
    dimensions: int = 1536
    embedded: list[str] = field(default_factory=list)

    def on(self, schema: type[BaseModel], handler: Handler) -> "FakeProvider":
        self.handlers[schema] = handler
        return self

    async def structured(
        self,
        *,
        instructions: str,
        input: str,
        schema: type[T],
        web_search: bool = False,
        allowed_domains: list[str] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMResult[T]:
        self.calls.append(FakeCall(schema.__name__, instructions, input, web_search, allowed_domains))
        handler = self.handlers.get(schema)
        if handler is None:
            raise AIError(f"FakeProvider has no handler for {schema.__name__}")
        value = handler(input)
        if isinstance(value, BaseException):
            raise value
        output = value if isinstance(value, schema) else schema.model_validate(value)
        usage = Usage(
            model=self.model,
            input_tokens=self.usage.input_tokens,
            cached_input_tokens=self.usage.cached_input_tokens,
            output_tokens=self.usage.output_tokens,
            web_search_calls=1 if web_search else 0,
        )
        return LLMResult(output=output, usage=usage, citations=list(self.citations) if web_search else [])

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Bag-of-words vectors (hashed words, unit length): texts sharing words come out similar, which is enough
        to test semantic retrieval without a model."""
        self.embedded += texts
        vectors = []
        for text in texts:
            v = [0.0] * self.dimensions
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dimensions] += 1.0  # noqa: S324 - not security
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vectors.append([x / norm for x in v])
        tokens = sum(len(t.split()) for t in texts)
        return EmbeddingResult(vectors, Usage(model="fake-embedding", input_tokens=tokens))
