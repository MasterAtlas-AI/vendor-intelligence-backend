"""The AI provider port. Pipelines depend on this, never on a vendor SDK, so OpenAI can be swapped for Azure OpenAI,
another model provider, or a scripted fake in tests.

A call is always *structured*: the provider must return an instance of the given Pydantic schema (or raise). Free
text never reaches the rest of the application.
"""

from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    web_search_calls: int = 0


@dataclass(frozen=True)
class Citation:
    url: str
    title: str = ""


@dataclass
class LLMResult(Generic[T]):  # noqa: UP046 - shared TypeVar with the Protocol below
    output: T
    usage: Usage
    # Pages the provider actually retrieved (web search). Extracted facts may only cite these.
    citations: list[Citation] = field(default_factory=list)


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]  # one per input text, same order
    usage: Usage


class AIError(Exception):
    """A call failed and retrying won't help (bad request, refusal, output that doesn't fit the schema).

    `usage` is set when the provider still billed the call (e.g. it answered, but not in the schema)."""

    def __init__(self, message: str, usage: "Usage | None" = None) -> None:
        super().__init__(message)
        self.usage = usage


class AIRetryable(AIError):
    """Rate limit, timeout, connection problem or provider 5xx: worth retrying with backoff."""


class AINotConfigured(AIError):
    """No provider is configured (e.g. OPENAI_API_KEY missing), or it rejected the credentials."""


class LLMProvider(Protocol):
    name: str
    model: str

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
    ) -> LLMResult[T]: ...

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Vectors for search (embedding_dimensions long, unit length)."""
        ...
