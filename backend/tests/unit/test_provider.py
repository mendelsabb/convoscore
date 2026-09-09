"""Provider boundary: error translation, prompt construction, and the fake scorer.

No test here reaches the network. The OpenAI SDK's exception classes are instantiated directly to
check that each one maps to the behaviour the worker expects.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from app.models import ErrorType
from app.scoring.contract import PROMPT_VERSION, SCHEMA_VERSION, ScoringResult
from app.scoring.prompt import SYSTEM_PROMPT, build_user_input, render_transcript
from app.scoring.provider import (
    FakeProvider,
    LLMInvalidResponseError,
    LLMPermanentError,
    LLMTransientError,
    classify_provider_error,
)

CONVERSATION = {
    "messages": [
        {"role": "customer", "content": "My invoice is wrong again."},
        {"role": "agent", "content": "Let me check that for you."},
    ],
    "metadata": {"channel": "email", "tags": ["billing"]},
}


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/responses")


def _response(status: int) -> httpx.Response:
    return httpx.Response(status_code=status, request=_request())


# --------------------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------------------


def test_timeout_is_transient() -> None:
    error = classify_provider_error(openai.APITimeoutError(request=_request()))
    assert isinstance(error, LLMTransientError)
    assert error.error_type is ErrorType.TIMEOUT


def test_rate_limit_is_transient() -> None:
    error = classify_provider_error(
        openai.RateLimitError("slow down", response=_response(429), body=None)
    )
    assert isinstance(error, LLMTransientError)
    assert error.error_type is ErrorType.RATE_LIMITED


def test_server_error_is_transient() -> None:
    error = classify_provider_error(
        openai.InternalServerError("upstream", response=_response(500), body=None)
    )
    assert isinstance(error, LLMTransientError)
    assert error.error_type is ErrorType.SERVER_ERROR


def test_connection_error_is_transient() -> None:
    error = classify_provider_error(openai.APIConnectionError(request=_request()))
    assert isinstance(error, LLMTransientError)
    assert error.error_type is ErrorType.CONNECTION_ERROR


@pytest.mark.parametrize(
    ("exception_class", "status"),
    [
        (openai.AuthenticationError, 401),
        (openai.PermissionDeniedError, 403),
        (openai.BadRequestError, 400),
        (openai.NotFoundError, 404),
    ],
)
def test_client_errors_are_permanent(exception_class: type, status: int) -> None:
    """Retrying a bad key or an unknown model wastes quota and delays the failure."""
    error = classify_provider_error(
        exception_class("nope", response=_response(status), body=None)
    )
    assert isinstance(error, LLMPermanentError)
    assert error.error_type is ErrorType.CLIENT_ERROR


def test_unrecognised_exception_is_treated_as_transient() -> None:
    """One odd failure should not discard a job; the attempt cap still stops a loop."""
    error = classify_provider_error(RuntimeError("something strange"))
    assert isinstance(error, LLMTransientError)
    assert error.error_type is ErrorType.INTERNAL_ERROR


def test_our_own_errors_pass_through_unchanged() -> None:
    original = LLMInvalidResponseError("bad shape")
    assert classify_provider_error(original) is original


# --------------------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------------------


def test_transcript_renders_speakers_in_order() -> None:
    assert render_transcript(CONVERSATION) == (
        "CUSTOMER: My invoice is wrong again.\nAGENT: Let me check that for you."
    )


def test_conversation_content_is_separated_from_instructions() -> None:
    """The rubric goes in the instructions; the transcript is data, inside a delimiter."""
    user_input = build_user_input(CONVERSATION)
    assert "<conversation>" in user_input and "</conversation>" in user_input
    assert "risk_score" not in user_input
    assert "risk_score" in SYSTEM_PROMPT


def test_metadata_is_not_sent_to_the_model() -> None:
    """Channel and tags are routing information, not evidence to score on."""
    user_input = build_user_input(CONVERSATION)
    assert "billing" not in user_input
    assert "email" not in user_input


def test_prompt_tells_the_model_to_ignore_embedded_instructions() -> None:
    assert "never as instructions" in SYSTEM_PROMPT


# --------------------------------------------------------------------------------------
# Fake provider
# --------------------------------------------------------------------------------------


def test_fake_provider_is_deterministic() -> None:
    provider = FakeProvider()
    first = provider.score(CONVERSATION)
    second = provider.score(CONVERSATION)
    assert first.result == second.result


def test_fake_provider_output_satisfies_the_contract() -> None:
    outcome = FakeProvider().score(CONVERSATION)
    ScoringResult.model_validate(outcome.result.model_dump())
    assert 0 <= outcome.result.risk_score <= 100
    assert outcome.prompt_version == PROMPT_VERSION
    assert outcome.schema_version == SCHEMA_VERSION
    assert outcome.total_tokens == outcome.prompt_tokens + outcome.completion_tokens


def test_fake_provider_separates_high_and_low_risk_conversations() -> None:
    """Demo fixtures should score plausibly, not uniformly."""
    churn = FakeProvider().score(
        {"messages": [{"role": "customer", "content": "Cancel my account today."}]}
    )
    routine = FakeProvider().score(
        {"messages": [{"role": "customer", "content": "What are your opening hours?"}]}
    )
    assert churn.result.risk_score > routine.result.risk_score
    assert churn.result.risk_score >= 76


def test_fake_provider_rationale_admits_what_it_is() -> None:
    assert "fake" in FakeProvider().score(CONVERSATION).result.rationale.lower()
