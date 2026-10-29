"""Cost estimate shown before a discovery runs. Deliberately pessimistic in `max_cost_usd`."""

from decimal import Decimal

from app.ai.ports import Usage
from app.ai.pricing import call_cost
from app.core.config import Settings
from app.discovery.schemas import DiscoveryEstimate, DiscoveryRequest

# Typical token use per call (input, output). Search was measured on gpt-5-mini (2026-10-29: 18.6k in, 2.75k out,
# 4 searches with search_context_size=low); the others are estimates. Recalibrate from ai_usage_events.
PLAN_TOKENS = (1_500, 400)
SEARCH_TOKENS = (19_000, 2_800)  # web search results count as input tokens
SEARCHES_PER_QUERY = 4
EXTRACT_TOKENS = (14_000, 2_500)
CLASSIFY_TOKENS = (5_000, 600)
SAFETY = 1.6


def query_count(req: DiscoveryRequest) -> int:
    return max(3, min(10, (req.max_candidates + 1) // 2))


def estimate(req: DiscoveryRequest, settings: Settings) -> DiscoveryEstimate:
    queries = query_count(req)
    model = settings.openai_model
    plan = call_cost(Usage(model, *_io(PLAN_TOKENS)), settings)
    search = (
        call_cost(Usage(model, *_io(SEARCH_TOKENS), web_search_calls=SEARCHES_PER_QUERY), settings) * queries
    )
    per_vendor = call_cost(Usage(model, *_io(EXTRACT_TOKENS)), settings) + call_cost(
        Usage(model, *_io(CLASSIFY_TOKENS)), settings
    )
    typical = float(plan + search + per_vendor * req.max_candidates)
    return DiscoveryEstimate(
        queries=queries,
        web_searches=queries * SEARCHES_PER_QUERY,
        candidates=req.max_candidates,
        typical_cost_usd=round(typical, 2),
        max_cost_usd=round(typical * SAFETY, 2),
    )


def _io(tokens: tuple[int, int]) -> tuple[int, int, int]:
    """(input, cached input, output) for Usage's positional fields."""
    return tokens[0], 0, tokens[1]


def capability_estimate(max_pages: int, settings: Settings) -> DiscoveryEstimate:
    """One known vendor: a profile extraction (bigger page budget) plus at most one classification call."""
    model = settings.openai_model
    extract = call_cost(Usage(model, *_io(EXTRACT_TOKENS)), settings) * Decimal(max(1.0, max_pages / 6))
    typical = float(extract + call_cost(Usage(model, *_io(CLASSIFY_TOKENS)), settings))
    return DiscoveryEstimate(queries=0, web_searches=0, candidates=1, typical_cost_usd=round(typical, 2),
                             max_cost_usd=round(typical * SAFETY, 2))  # fmt: skip
