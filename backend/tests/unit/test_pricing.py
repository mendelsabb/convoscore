"""Cost estimation. Wrong numbers presented as fact would be worse than no numbers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.scoring.pricing import PRICES, estimate_cost, price_for


def test_known_model_cost_is_exact() -> None:
    # gpt-4.1-mini: $0.40 per 1M input, $1.60 per 1M output.
    # 1000 input  -> 0.0004 ; 500 output -> 0.0008 ; total 0.0012
    assert estimate_cost("gpt-4.1-mini", 1000, 500) == Decimal("0.00120000")


def test_cost_scales_linearly() -> None:
    single = estimate_cost("gpt-4.1-mini", 1000, 500)
    double = estimate_cost("gpt-4.1-mini", 2000, 1000)
    assert double == single * 2


def test_zero_usage_is_zero_not_none() -> None:
    assert estimate_cost("gpt-4.1-mini", 0, 0) == Decimal("0E-8")


def test_dated_snapshot_names_resolve_to_the_base_model() -> None:
    """A pinned snapshot must not silently become an unpriced model."""
    assert price_for("gpt-4.1-mini-2025-04-14") == PRICES["gpt-4.1-mini"]
    assert estimate_cost("gpt-4.1-mini-2025-04-14", 1000, 500) == Decimal("0.00120000")


def test_unknown_model_returns_none_rather_than_guessing() -> None:
    assert price_for("some-future-model") is None
    assert estimate_cost("some-future-model", 1000, 500) is None


@pytest.mark.parametrize(
    ("prompt_tokens", "completion_tokens"),
    [(None, 500), (1000, None), (None, None)],
)
def test_missing_usage_returns_none(
    prompt_tokens: int | None, completion_tokens: int | None
) -> None:
    assert estimate_cost("gpt-4.1-mini", prompt_tokens, completion_tokens) is None


def test_result_fits_the_database_column() -> None:
    """estimated_cost_usd is NUMERIC(12,8); an over-precise value would be silently rounded."""
    cost = estimate_cost("gpt-5-nano", 3, 1)
    assert cost is not None
    assert -cost.as_tuple().exponent <= 8


def test_every_price_is_positive() -> None:
    for model, price in PRICES.items():
        assert price.input_per_million > 0, model
        assert price.output_per_million > 0, model
