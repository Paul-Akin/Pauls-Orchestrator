"""Cost = (N_in * p_in + N_out * p_out) / 1e6, prices in USD per 1M tokens.

Prices come ONLY from ``routes.yaml``. If either price is missing the cost is
reported as "price not configured"; we never fall back to a built-in table.
"""

from __future__ import annotations

from hub.config import Price
from hub.types import Cost, CostStatus, Usage


def compute_cost(price: Price, usage: Usage | None) -> Cost:
    if usage is None:
        return Cost(usd=None, status=CostStatus.USAGE_UNAVAILABLE)
    if price.input_per_mtok is None or price.output_per_mtok is None:
        return Cost(usd=None, status=CostStatus.PRICE_NOT_CONFIGURED)
    usd = (
        usage.input_tokens * price.input_per_mtok + usage.output_tokens * price.output_per_mtok
    ) / 1_000_000
    return Cost(usd=round(usd, 8), status=CostStatus.OK)
