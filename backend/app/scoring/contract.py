"""The scoring contract: what we ask the model for, and what the numbers mean.

Documented in docs/scoring-rubric.md. The versions here are stored on every result so that a
prompt or schema change never silently mixes with older scores.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROMPT_VERSION = "v1"
SCHEMA_VERSION = "1"

RATIONALE_MAX_CHARS = 300


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class RiskBand(StrEnum):
    LOW = "low_concern"
    MODERATE = "moderate_concern"
    HIGH = "high_concern"
    URGENT = "urgent_escalation"


# Upper bound (inclusive) of each band. Bands are reviewer guidance derived from the score;
# the model returns only the number.
_BAND_UPPER_BOUNDS: tuple[tuple[int, RiskBand], ...] = (
    (25, RiskBand.LOW),
    (50, RiskBand.MODERATE),
    (75, RiskBand.HIGH),
    (100, RiskBand.URGENT),
)

BAND_LABELS: dict[RiskBand, str] = {
    RiskBand.LOW: "Low concern",
    RiskBand.MODERATE: "Moderate concern",
    RiskBand.HIGH: "High concern",
    RiskBand.URGENT: "Urgent / escalation",
}


def risk_band(risk_score: int | None) -> RiskBand | None:
    """Map a 0-100 risk score onto its band. ``None`` for jobs that have no score yet."""
    if risk_score is None:
        return None
    if not 0 <= risk_score <= 100:
        raise ValueError(f"risk_score must be between 0 and 100, got {risk_score}")
    for upper, band in _BAND_UPPER_BOUNDS:
        if risk_score <= upper:
            return band
    raise AssertionError("unreachable: bands cover 0-100")


class ScoringResult(BaseModel):
    """The structured output we require from the model, and re-validate on the way in.

    ``extra="forbid"`` matters twice: it is what OpenAI strict structured outputs require, and it
    makes a model that invents extra fields a validation failure rather than a silent success.
    """

    model_config = ConfigDict(extra="forbid")

    sentiment: Literal["positive", "neutral", "negative"] = Field(
        description="The customer's overall tone across the conversation, not the agent's."
    )
    risk_score: int = Field(
        ge=0,
        le=100,
        description=(
            "How likely this account needs human intervention beyond the conversation: churn, "
            "escalation, financial, legal, safety or reputational exposure. 0-25 low, 26-50 "
            "moderate, 51-75 high, 76-100 urgent."
        ),
    )
    rationale: str = Field(
        min_length=1,
        max_length=RATIONALE_MAX_CHARS,
        description=(
            "At most two sentences naming the concrete trigger in the conversation, readable by a "
            "support lead who has not seen it."
        ),
    )

    @property
    def band(self) -> RiskBand:
        band = risk_band(self.risk_score)
        assert band is not None  # risk_score is validated 0-100
        return band
