"""Model pricing and cost estimation.

These are list prices in USD per million tokens, recorded on the date in ``PRICING_VERSION``.
They are an *estimate*: they ignore batch discounts, cached-input pricing and negotiated rates.
Production billing must be reconciled against the provider's invoices; this table exists so that
cost is visible per job and on the dashboard, not so that it can be used for accounting.

To update: change the numbers, bump PRICING_VERSION. Existing rows keep the version they were
scored under, so historical estimates stay explainable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

PRICING_VERSION = "2026-09"

_PER_MILLION = Decimal("1000000")
# Cost is stored in NUMERIC(12,8); quantise to match so values round-trip exactly.
_COST_PRECISION = Decimal("0.00000001")


@dataclass(frozen=True)
class ModelPrice:
    """USD per one million tokens."""

    input_per_million: Decimal
    output_per_million: Decimal


PRICES: dict[str, ModelPrice] = {
    "gpt-4.1-mini": ModelPrice(Decimal("0.40"), Decimal("1.60")),
    "gpt-4.1-nano": ModelPrice(Decimal("0.10"), Decimal("0.40")),
    "gpt-4o-mini": ModelPrice(Decimal("0.15"), Decimal("0.60")),
    "gpt-5-mini": ModelPrice(Decimal("0.25"), Decimal("2.00")),
    "gpt-5-nano": ModelPrice(Decimal("0.05"), Decimal("0.40")),
}


def price_for(model: str) -> ModelPrice | None:
    """Look up a price, tolerating dated snapshot names like ``gpt-4.1-mini-2025-04-14``."""
    if model in PRICES:
        return PRICES[model]
    for name, price in PRICES.items():
        if model.startswith(name):
            return price
    return None


def estimate_cost(
    model: str, prompt_tokens: int | None, completion_tokens: int | None
) -> Decimal | None:
    """Estimated USD cost of one scoring call.

    Returns ``None`` for an unknown model or missing usage rather than guessing: a wrong number
    presented as fact is worse than an honest gap, and the caller records a metric for it.
    """
    price = price_for(model)
    if price is None or prompt_tokens is None or completion_tokens is None:
        return None

    cost = (
        Decimal(prompt_tokens) * price.input_per_million
        + Decimal(completion_tokens) * price.output_per_million
    ) / _PER_MILLION
    return cost.quantize(_COST_PRECISION, rounding=ROUND_HALF_UP)
