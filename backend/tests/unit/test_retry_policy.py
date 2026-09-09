"""Retry policy: what is worth retrying, and how long to wait.

Getting this wrong is expensive in both directions: retrying a permanent error burns quota and
delays the failure, while giving up on a transient one loses work that would have succeeded.
"""

from __future__ import annotations

import pytest

from app.models import ErrorType
from app.scoring.provider import (
    LLMInvalidResponseError,
    LLMPermanentError,
    LLMTransientError,
)
from app.worker import backoff_seconds, decide_retry

BASE, MAX = 5, 60


def decide(error, attempt: int, max_attempts: int = 3):
    # jitter fixed at its maximum so delays are deterministic in tests
    return decide_retry(error, attempt, max_attempts, BASE, MAX, jitter=lambda: 1.0)


@pytest.mark.parametrize(
    "error_type",
    [
        ErrorType.TIMEOUT,
        ErrorType.RATE_LIMITED,
        ErrorType.SERVER_ERROR,
        ErrorType.CONNECTION_ERROR,
    ],
)
def test_transient_errors_retry_until_the_cap(error_type: ErrorType) -> None:
    error = LLMTransientError("boom", error_type)
    assert decide(error, attempt=1).retry is True
    assert decide(error, attempt=2).retry is True

    exhausted = decide(error, attempt=3)
    assert exhausted.retry is False
    assert exhausted.error_type is ErrorType.MAX_ATTEMPTS_EXHAUSTED


def test_transient_error_keeps_its_own_type_while_retrying() -> None:
    decision = decide(LLMTransientError("slow", ErrorType.TIMEOUT), attempt=1)
    assert decision.error_type is ErrorType.TIMEOUT


def test_permanent_errors_are_never_retried() -> None:
    """A bad key or malformed request fails identically every time."""
    decision = decide(LLMPermanentError("invalid api key"), attempt=1)
    assert decision.retry is False
    assert decision.error_type is ErrorType.CLIENT_ERROR


def test_invalid_response_gets_exactly_one_more_chance() -> None:
    """Once can be a bad sample; twice means the prompt or schema is wrong."""
    error = LLMInvalidResponseError("risk_score was 250")
    assert decide(error, attempt=1).retry is True
    assert decide(error, attempt=2).retry is False
    assert decide(error, attempt=2).error_type is ErrorType.INVALID_LLM_RESPONSE


def test_invalid_response_respects_a_lower_attempt_cap() -> None:
    error = LLMInvalidResponseError("bad shape")
    assert decide(error, attempt=1, max_attempts=1).retry is False


def test_backoff_grows_exponentially_and_is_capped() -> None:
    full = [backoff_seconds(n, BASE, MAX, jitter=lambda: 1.0) for n in (1, 2, 3, 4, 5, 6)]
    assert full[:4] == [5, 10, 20, 40]
    assert all(value <= MAX for value in full)
    assert full[-1] == MAX


def test_backoff_is_jittered_within_half_the_window() -> None:
    """Jitter stops several workers retrying in lockstep against a struggling provider."""
    lowest = backoff_seconds(3, BASE, MAX, jitter=lambda: 0.0)
    highest = backoff_seconds(3, BASE, MAX, jitter=lambda: 1.0)
    assert lowest == 10 and highest == 20


def test_backoff_is_never_zero() -> None:
    assert backoff_seconds(1, 1, 60, jitter=lambda: 0.0) >= 1
