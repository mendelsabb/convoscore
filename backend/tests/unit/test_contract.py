"""The scoring contract: bands, validation, and what we refuse to accept from a model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.scoring.contract import (
    BAND_LABELS,
    RATIONALE_MAX_CHARS,
    RiskBand,
    ScoringResult,
    risk_band,
)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, RiskBand.LOW),
        (25, RiskBand.LOW),
        (26, RiskBand.MODERATE),
        (50, RiskBand.MODERATE),
        (51, RiskBand.HIGH),
        (75, RiskBand.HIGH),
        (76, RiskBand.URGENT),
        (100, RiskBand.URGENT),
    ],
)
def test_band_boundaries(score: int, expected: RiskBand) -> None:
    assert risk_band(score) is expected


def test_band_is_none_for_unscored_job() -> None:
    assert risk_band(None) is None


@pytest.mark.parametrize("score", [-1, 101, 1000])
def test_band_rejects_out_of_range(score: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        risk_band(score)


def test_every_band_has_a_human_label() -> None:
    assert set(BAND_LABELS) == set(RiskBand)


def test_valid_result_exposes_its_band() -> None:
    result = ScoringResult(sentiment="negative", risk_score=80, rationale="Threatened to cancel.")
    assert result.band is RiskBand.URGENT


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"sentiment": "angry", "risk_score": 50, "rationale": "x"}, id="bad-sentiment"
        ),
        pytest.param(
            {"sentiment": "negative", "risk_score": 250, "rationale": "x"}, id="risk-high"
        ),
        pytest.param({"sentiment": "negative", "risk_score": -5, "rationale": "x"}, id="risk-low"),
        pytest.param({"sentiment": "negative", "risk_score": 50, "rationale": ""}, id="empty"),
        pytest.param({"sentiment": "negative", "risk_score": 50}, id="missing-rationale"),
    ],
)
def test_invalid_model_output_is_rejected(payload: dict) -> None:
    """These are exactly the shapes the malformed-response failure injection produces."""
    with pytest.raises(ValidationError):
        ScoringResult.model_validate(payload)


def test_extra_fields_are_rejected() -> None:
    """A model that invents fields is a validation failure, not a silent success."""
    with pytest.raises(ValidationError):
        ScoringResult.model_validate(
            {
                "sentiment": "neutral",
                "risk_score": 10,
                "rationale": "Routine question.",
                "confidence": 0.9,
            }
        )


def test_rationale_is_bounded() -> None:
    with pytest.raises(ValidationError):
        ScoringResult(
            sentiment="neutral", risk_score=10, rationale="x" * (RATIONALE_MAX_CHARS + 1)
        )
