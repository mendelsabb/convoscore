"""Deterministic failure injection.

The property under test is not "the demo works" but "the demo causes a *real* failure". Injection
happens at the provider boundary; everything downstream is the production code path, and these
tests assert exactly that: attempts are counted, jobs are retried and then failed, and metrics move
because real work failed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app import demo
from app.demo import MALFORMED_RESPONSE, FaultInjectingProvider
from app.models import ErrorType, FailureMode
from app.scoring.contract import ScoringResult
from app.scoring.provider import (
    FakeProvider,
    LLMInvalidResponseError,
    LLMTransientError,
)
from tests.conftest import requires_postgres

pytestmark = [requires_postgres, pytest.mark.integration]


@pytest.fixture
def provider(session: Session) -> FaultInjectingProvider:
    # No sleep in tests: the delay exists to be visible on a dashboard, not to slow a test suite.
    return FaultInjectingProvider(FakeProvider(), lambda: session, timeout_seconds=0)


CONVERSATION = {"messages": [{"role": "customer", "content": "My order never arrived."}]}


# --------------------------------------------------------------------------------------
# Token arithmetic
# --------------------------------------------------------------------------------------


def test_nothing_is_armed_by_default(session: Session) -> None:
    armed = demo.state(session)
    assert armed.total == 0
    assert set(armed.modes) == {mode.value for mode in FailureMode}


def test_arming_sets_a_count(session: Session) -> None:
    demo.arm(session, FailureMode.TIMEOUT, 3)
    session.commit()
    assert demo.state(session).modes["timeout"] == 3


def test_arming_replaces_rather_than_accumulates(session: Session) -> None:
    """Arming twice sets the count; it does not add to it, which would surprise a presenter."""
    demo.arm(session, FailureMode.TIMEOUT, 3)
    demo.arm(session, FailureMode.TIMEOUT, 1)
    session.commit()
    assert demo.state(session).modes["timeout"] == 1


def test_each_token_is_consumed_exactly_once(session: Session) -> None:
    """The guarantee that makes the demo deterministic with two worker replicas."""
    demo.arm(session, FailureMode.HTTP_500, 2)
    session.commit()

    assert demo.consume(session) is FailureMode.HTTP_500
    assert demo.consume(session) is FailureMode.HTTP_500
    assert demo.consume(session) is None

    assert demo.state(session).total == 0


def test_clearing_disarms_everything(session: Session) -> None:
    demo.arm(session, FailureMode.TIMEOUT, 5)
    demo.arm(session, FailureMode.MALFORMED, 5)
    session.commit()

    demo.clear(session)
    session.commit()

    assert demo.state(session).total == 0
    assert demo.consume(session) is None


def test_count_cannot_go_negative(session: Session) -> None:
    demo.arm(session, FailureMode.TIMEOUT, 1)
    session.commit()
    demo.consume(session)
    demo.consume(session)
    session.commit()
    assert demo.state(session).modes["timeout"] == 0


# --------------------------------------------------------------------------------------
# The provider wrapper
# --------------------------------------------------------------------------------------


def test_pass_through_when_nothing_is_armed(provider: FaultInjectingProvider) -> None:
    """Leaving the wrapper installed must cost nothing."""
    outcome = provider.score(CONVERSATION)
    assert outcome.result.risk_score >= 0


def test_timeout_mode_raises_a_real_transient_timeout(
    provider: FaultInjectingProvider, session: Session
) -> None:
    demo.arm(session, FailureMode.TIMEOUT, 1)
    session.commit()

    with pytest.raises(LLMTransientError) as raised:
        provider.score(CONVERSATION)

    # Classified exactly like a genuine provider timeout, so the retry policy treats it the same.
    assert raised.value.error_type is ErrorType.TIMEOUT


def test_server_error_mode_raises_a_real_transient_error(
    provider: FaultInjectingProvider, session: Session
) -> None:
    demo.arm(session, FailureMode.HTTP_500, 1)
    session.commit()

    with pytest.raises(LLMTransientError) as raised:
        provider.score(CONVERSATION)

    assert raised.value.error_type is ErrorType.SERVER_ERROR


def test_malformed_mode_is_rejected_by_the_real_validator(
    provider: FaultInjectingProvider, session: Session
) -> None:
    """The failure comes from the contract, not from a hand-thrown exception."""
    demo.arm(session, FailureMode.MALFORMED, 1)
    session.commit()

    with pytest.raises(LLMInvalidResponseError):
        provider.score(CONVERSATION)

    # The same payload fails the same validator that guards real responses.
    with pytest.raises(ValidationError):
        ScoringResult.model_validate(MALFORMED_RESPONSE)


def test_injection_is_spent_after_one_call(
    provider: FaultInjectingProvider, session: Session
) -> None:
    demo.arm(session, FailureMode.HTTP_500, 1)
    session.commit()

    with pytest.raises(LLMTransientError):
        provider.score(CONVERSATION)

    # The next call succeeds, which is what makes "one failure then recovery" demonstrable.
    assert provider.score(CONVERSATION).result.risk_score >= 0


def test_a_database_failure_does_not_break_scoring(session: Session) -> None:
    """The demo is a convenience; the pipeline is the product."""

    def broken_session():
        raise RuntimeError("database unavailable")

    provider = FaultInjectingProvider(FakeProvider(), broken_session, timeout_seconds=0)
    assert provider.score(CONVERSATION).result.risk_score >= 0


def test_wrapper_name_shows_it_is_installed(provider: FaultInjectingProvider) -> None:
    assert provider.name == "fake+demo"
