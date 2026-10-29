from decimal import ROUND_HALF_UP, Decimal

from app.ai.ports import Usage
from app.core.config import Settings

MTOK = Decimal(1_000_000)
MICRO = Decimal("0.000001")


def call_cost(usage: Usage, settings: Settings) -> Decimal:
    """US$ cost of one call from its token and tool usage. Cached input tokens are billed at the cached rate."""
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    cost = (
        Decimal(uncached) * Decimal(str(settings.ai_price_input_per_mtok)) / MTOK
        + Decimal(usage.cached_input_tokens) * Decimal(str(settings.ai_price_cached_input_per_mtok)) / MTOK
        + Decimal(usage.output_tokens) * Decimal(str(settings.ai_price_output_per_mtok)) / MTOK
        + Decimal(usage.web_search_calls) * Decimal(str(settings.ai_price_per_web_search))
    )
    return cost.quantize(MICRO, rounding=ROUND_HALF_UP)
