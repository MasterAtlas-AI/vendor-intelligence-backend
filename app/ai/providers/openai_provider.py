"""OpenAI provider: Responses API with Structured Outputs and the hosted `web_search` tool."""

from typing import Any, TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.ai.ports import AIError, AINotConfigured, AIRetryable, Citation, EmbeddingResult, LLMResult, Usage
from app.core.config import Settings

T = TypeVar("T", bound=BaseModel)


def citations_from(response: Any) -> list[Citation]:
    """URL citations attached to the answer, de-duplicated, in order of first appearance."""
    seen: dict[str, Citation] = {}
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            for ann in getattr(part, "annotations", None) or []:
                if getattr(ann, "type", None) == "url_citation" and ann.url not in seen:
                    seen[ann.url] = Citation(url=ann.url, title=getattr(ann, "title", "") or "")
    return list(seen.values())


def usage_from(response: Any, model: str) -> Usage:
    u = getattr(response, "usage", None)
    details = getattr(u, "input_tokens_details", None)
    calls = sum(
        1
        for item in getattr(response, "output", None) or []
        if getattr(item, "type", None) == "web_search_call"
    )
    return Usage(
        model=getattr(response, "model", None) or model,
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        cached_input_tokens=getattr(details, "cached_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        web_search_calls=calls,
    )


def refusal_from(response: Any) -> str | None:
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return str(getattr(part, "refusal", "") or "refused")
    return None


def translate(exc: openai.OpenAIError) -> AIError:
    """SDK errors -> our error types: bad credentials, retryable (rate limit, timeout, 5xx), or a plain failure."""
    if isinstance(exc, openai.AuthenticationError):
        return AINotConfigured("OpenAI rejected the API key")
    if isinstance(exc, openai.PermissionDeniedError):
        return AINotConfigured(f"OpenAI denied access: {exc.message}")
    if isinstance(
        exc,
        (
            openai.RateLimitError,
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,
        ),
    ):
        return AIRetryable(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, openai.APIStatusError):
        return AIError(f"OpenAI error {exc.status_code}: {exc.message}")
    return AIError(f"OpenAI error: {exc}")


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.model = settings.openai_model
        self.max_searches = settings.ai_max_searches_per_call
        self.search_context_size = settings.ai_search_context_size
        self.embedding_model = settings.openai_embedding_model
        self.embedding_dimensions = settings.embedding_dimensions
        if client is None:
            if settings.openai_api_key is None:
                raise AINotConfigured("OPENAI_API_KEY is not set")
            client = AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                base_url=settings.openai_base_url,
                timeout=settings.openai_timeout_seconds,
                max_retries=0,  # AiService owns retries, so each attempt is metered and logged
            )
        self.client = client

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
        tools: list[dict[str, Any]] = []
        if web_search:
            tool: dict[str, Any] = {"type": "web_search", "search_context_size": self.search_context_size}
            if allowed_domains:
                tool["filters"] = {"allowed_domains": allowed_domains}
            tools.append(tool)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": input,
            "text_format": schema,
            "store": False,  # nothing kept on the provider side
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["max_tool_calls"] = self.max_searches  # each search costs money and adds result tokens
        if max_output_tokens:
            kwargs["max_output_tokens"] = max_output_tokens
        if reasoning_effort and self.model.startswith(("gpt-5", "o")):  # only reasoning models take it
            kwargs["reasoning"] = {"effort": reasoning_effort}
        try:
            response = await self.client.responses.parse(**kwargs)
        except openai.OpenAIError as exc:
            raise translate(exc) from exc

        usage = usage_from(response, self.model)
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            if refusal := refusal_from(response):
                raise AIError(f"The model declined: {refusal}", usage)
            status = getattr(response, "status", None)
            raise AIError(f"No structured output (status {status})", usage)
        return LLMResult(output=parsed, usage=usage, citations=citations_from(response))

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        try:
            response = await self.client.embeddings.create(
                model=self.embedding_model, input=texts, dimensions=self.embedding_dimensions
            )
        except openai.OpenAIError as exc:
            raise translate(exc) from exc
        vectors = [d.embedding for d in sorted(response.data, key=lambda d: d.index)]
        if len(vectors) != len(texts):
            raise AIError(f"Expected {len(texts)} embeddings, got {len(vectors)}")
        tokens = getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0
        return EmbeddingResult(vectors, Usage(model=getattr(response, "model", None) or self.embedding_model,
                                              input_tokens=tokens))  # fmt: skip
