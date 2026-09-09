"""LLM providers and the error taxonomy the worker reacts to.

OpenAI is treated as an unreliable external dependency. Everything it can do wrong is translated
here into one of three application errors, so the worker never imports the OpenAI SDK and can be
tested against a deterministic fake:

* ``LLMTransientError``      - worth retrying (timeout, rate limit, 5xx, connection)
* ``LLMPermanentError``      - retrying cannot help (bad request, auth, unknown model)
* ``LLMInvalidResponseError``- the model answered but not in the contracted shape

The provider's own retries are disabled (``max_retries=0``): the application owns retry policy so
that every attempt is a database row and a metric, not an invisible loop inside the SDK.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from pydantic import ValidationError

from app.logging import get_logger
from app.models import ErrorType
from app.scoring.contract import PROMPT_VERSION, SCHEMA_VERSION, ScoringResult
from app.scoring.pricing import PRICING_VERSION, estimate_cost
from app.scoring.prompt import SYSTEM_PROMPT, build_user_input

log = get_logger(__name__)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class LLMError(Exception):
    """Base for everything a provider can fail with."""

    error_type: ErrorType = ErrorType.INTERNAL_ERROR


class LLMTransientError(LLMError):
    """Temporary: the same request may well succeed later."""

    def __init__(self, message: str, error_type: ErrorType) -> None:
        super().__init__(message)
        self.error_type = error_type


class LLMPermanentError(LLMError):
    """Retrying will not help: a bad request, bad credentials, or an unknown model."""

    error_type = ErrorType.CLIENT_ERROR


class LLMInvalidResponseError(LLMError):
    """The model responded, but not with a result that satisfies the contract."""

    error_type = ErrorType.INVALID_LLM_RESPONSE


# --------------------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringOutcome:
    """A validated score plus everything worth recording about how it was produced."""

    result: ScoringResult
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    latency_ms: int
    estimated_cost_usd: Decimal | None
    prompt_version: str = PROMPT_VERSION
    schema_version: str = SCHEMA_VERSION
    pricing_version: str = PRICING_VERSION


class LLMProvider(Protocol):
    """What the worker needs from a scoring backend."""

    name: str

    def score(self, conversation: dict[str, Any]) -> ScoringOutcome:
        """Score a conversation, or raise an ``LLMError``."""
        ...


# --------------------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------------------


class OpenAIProvider:
    """Scores via OpenAI structured outputs.

    The shape is enforced by the API and re-checked here, because the contract is ours.
    """

    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        temperature: float | None = 0.0,
        base_url: str | None = None,
    ) -> None:
        from openai import OpenAI

        self.model = model
        self.timeout_seconds = timeout_seconds
        # None means "do not send the parameter at all": reasoning models reject temperature.
        self.temperature = temperature
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    def score(self, conversation: dict[str, Any]) -> ScoringOutcome:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": build_user_input(conversation),
            "text_format": ScoringResult,
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        started = time.monotonic()
        try:
            response = self._client.responses.parse(**kwargs)
        except Exception as exc:  # translated below into our own taxonomy
            raise classify_provider_error(exc) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            # A refusal or a truncated response lands here.
            raise LLMInvalidResponseError(
                "model returned no parsable result "
                f"(status={getattr(response, 'status', 'unknown')})"
            )

        # Re-validate even though the API enforced the schema: the contract is ours, not theirs.
        try:
            result = ScoringResult.model_validate(parsed.model_dump())
        except ValidationError as exc:
            raise LLMInvalidResponseError(f"response failed contract validation: {exc}") from exc

        prompt_tokens, completion_tokens, total_tokens = _usage(response)
        # Price the model that actually ran, not the one we asked for. Requesting "gpt-4.1-mini"
        # returns a dated snapshot, and an alias could one day resolve to a differently priced
        # model; the recorded cost should follow the response.
        served_model = getattr(response, "model", None) or self.model
        return ScoringOutcome(
            result=result,
            model=served_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            estimated_cost_usd=estimate_cost(served_model, prompt_tokens, completion_tokens),
        )


def _usage(response: Any) -> tuple[int | None, int | None, int | None]:
    """Pull token usage off a response, tolerating its absence."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None, None
    prompt_tokens = getattr(usage, "input_tokens", None)
    completion_tokens = getattr(usage, "output_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    return prompt_tokens, completion_tokens, total_tokens


def classify_provider_error(exc: Exception) -> LLMError:
    """Translate an SDK exception into the taxonomy the worker acts on.

    Imported lazily so that this module, and the worker, stay usable without the OpenAI SDK.
    """
    if isinstance(exc, LLMError):
        return exc

    try:
        import openai
    except ImportError:  # pragma: no cover - the SDK is a runtime dependency
        return LLMTransientError(str(exc), ErrorType.INTERNAL_ERROR)

    if isinstance(exc, openai.APITimeoutError):
        return LLMTransientError(str(exc), ErrorType.TIMEOUT)
    if isinstance(exc, openai.RateLimitError):
        return LLMTransientError(str(exc), ErrorType.RATE_LIMITED)
    if isinstance(exc, openai.InternalServerError):
        return LLMTransientError(str(exc), ErrorType.SERVER_ERROR)
    if isinstance(exc, openai.APIConnectionError):
        return LLMTransientError(str(exc), ErrorType.CONNECTION_ERROR)
    if isinstance(
        exc,
        openai.AuthenticationError
        | openai.PermissionDeniedError
        | openai.BadRequestError
        | openai.NotFoundError,
    ):
        # Bad key, no access, malformed request, unknown model: retrying burns money and time.
        return LLMPermanentError(str(exc))
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status >= 500:
            return LLMTransientError(str(exc), ErrorType.SERVER_ERROR)
        if status == 429:
            return LLMTransientError(str(exc), ErrorType.RATE_LIMITED)
        return LLMPermanentError(str(exc))

    # Unrecognised: treat as transient so a single odd failure does not discard the job, but the
    # attempt cap still stops it from retrying forever.
    return LLMTransientError(f"{type(exc).__name__}: {exc}", ErrorType.INTERNAL_ERROR)


# --------------------------------------------------------------------------------------
# Fake
# --------------------------------------------------------------------------------------


class FakeProvider:
    """Deterministic scorer for tests and for running the whole stack at zero cost.

    The score is derived from the conversation content, so the same transcript always produces the
    same result, and different transcripts produce visibly different ones. It is a stand-in for a
    model, not a pretend one: nothing here claims to be a real judgement.
    """

    name = "fake"

    def __init__(self, model: str = "fake-model", latency_ms: int = 5) -> None:
        self.model = model
        self.latency_ms = latency_ms

    def score(self, conversation: dict[str, Any]) -> ScoringOutcome:
        transcript = build_user_input(conversation)
        digest = hashlib.sha256(transcript.encode("utf-8")).digest()

        # Keyword signals first, so demo fixtures score plausibly; hash only breaks ties.
        lowered = transcript.lower()
        risk = digest[0] % 40  # 0-39 baseline
        if any(word in lowered for word in ("cancel", "lawyer", "legal", "breach", "sue")):
            risk = 76 + digest[1] % 24
        elif any(word in lowered for word in ("refund", "manager", "supervisor", "unacceptable")):
            risk = 51 + digest[1] % 25
        elif any(word in lowered for word in ("still", "again", "third time", "waiting")):
            risk = 26 + digest[1] % 25

        if risk >= 51:
            sentiment = "negative"
        elif risk >= 26:
            sentiment = "neutral"
        else:
            sentiment = "positive" if digest[2] % 2 else "neutral"

        prompt_tokens = max(1, len(transcript) // 4)
        completion_tokens = 40
        return ScoringOutcome(
            result=ScoringResult(
                sentiment=sentiment,
                risk_score=risk,
                rationale=(
                    "Deterministic fake score derived from the conversation content "
                    "(no model was called)."
                ),
            ),
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            latency_ms=self.latency_ms,
            estimated_cost_usd=Decimal("0E-8"),
        )
