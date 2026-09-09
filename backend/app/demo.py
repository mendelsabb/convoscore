"""Deterministic failure injection, for demonstrations only.

The point of this module is that it does **not** fake anything downstream. Arming a failure makes
the next scoring call genuinely fail at the provider boundary; everything after that is the
ordinary production code path:

    provider raises → error classified → retry decision → visibility backoff → attempt recorded
    → status transition in PostgreSQL → counters incremented → Prometheus scrapes → Grafana moves

Nothing here writes to Prometheus, edits a dashboard, or special-cases the worker. A demo that
nudged the metrics directly would prove only that the demo works.

Two design choices make it reliable in front of an audience:

* **Tokens live in the database.** There are two worker replicas. An in-memory flag would only
  affect whichever pod happened to pick up the job, so the demo would be a coin toss. A row is
  shared, and decrementing it is atomic, so exactly one call consumes each token.
* **The real provider is never called when a token is consumed.** The failure is simulated at the
  boundary rather than by making a deliberately broken request, so demonstrating failure costs
  nothing and cannot depend on how OpenAI happens to behave that afternoon.

Everything here is gated on ``DEMO_MODE``. With it off the routes are not mounted and the worker
uses the provider directly.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import metrics
from app.logging import get_logger
from app.models import DemoFailureToken, ErrorType, FailureMode
from app.scoring.contract import ScoringResult
from app.scoring.provider import (
    LLMInvalidResponseError,
    LLMProvider,
    LLMTransientError,
    ScoringOutcome,
)

log = get_logger(__name__)

# How long a simulated timeout waits before failing. Long enough to appear in the latency
# histogram and to be visible on screen, short enough not to stall a live demo.
SIMULATED_TIMEOUT_SECONDS = 2.0

# A response that violates the contract in two ways at once: an invalid sentiment and a risk score
# outside 0-100. It is rejected by the same validator that guards real responses.
MALFORMED_RESPONSE: dict[str, Any] = {
    "sentiment": "furious",
    "risk_score": 250,
    "rationale": "Deliberately malformed response, injected for the demo.",
}


@dataclass(frozen=True)
class ArmedFailures:
    modes: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.modes.values())


def arm(session: Session, mode: FailureMode, count: int) -> int:
    """Arm ``count`` failures of ``mode``, replacing whatever was armed before.

    Returns the number now armed. Setting count to 0 disarms that mode.
    """
    statement = (
        insert(DemoFailureToken)
        .values(mode=mode.value, remaining=count, armed_at=time_now())
        .on_conflict_do_update(
            index_elements=[DemoFailureToken.mode],
            set_={"remaining": count, "armed_at": time_now()},
        )
    )
    session.execute(statement)
    log.info("demo_failure_armed", extra={"mode": mode.value, "count": count})
    return count


def clear(session: Session) -> None:
    session.execute(update(DemoFailureToken).values(remaining=0))
    log.info("demo_failure_cleared")


def state(session: Session) -> ArmedFailures:
    rows = session.execute(
        select(DemoFailureToken.mode, DemoFailureToken.remaining)
    ).all()
    armed = {mode.value: 0 for mode in FailureMode}
    armed.update({mode: int(remaining) for mode, remaining in rows})
    return ArmedFailures(armed)


def consume(session: Session) -> FailureMode | None:
    """Take one token, if any are armed. Atomic, so two workers cannot consume the same one.

    Modes are checked in a fixed order so that a demo with several modes armed is reproducible.
    """
    for mode in FailureMode:
        claimed = session.execute(
            update(DemoFailureToken)
            .where(DemoFailureToken.mode == mode.value, DemoFailureToken.remaining > 0)
            .values(remaining=DemoFailureToken.remaining - 1)
            .returning(DemoFailureToken.remaining)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if claimed is not None:
            log.warning(
                "demo_failure_consumed", extra={"mode": mode.value, "remaining": claimed}
            )
            return mode
    return None


def time_now():  # noqa: ANN201 - thin wrapper kept for patching in tests
    from sqlalchemy import func

    return func.now()


class FaultInjectingProvider:
    """Wraps a real provider and fails on demand.

    When no token is armed this is a pass-through: the wrapped provider is called exactly as it
    would be otherwise, so leaving the wrapper installed costs nothing.
    """

    def __init__(
        self,
        inner: LLMProvider,
        session_factory: Callable[[], Session],
        timeout_seconds: float = SIMULATED_TIMEOUT_SECONDS,
    ) -> None:
        self._inner = inner
        self._session_factory = session_factory
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return f"{self._inner.name}+demo"

    def _next_failure(self) -> FailureMode | None:
        """Consume a token in its own committed transaction.

        A failure to reach the database here must not break scoring: the demo is a convenience,
        the pipeline is the product.
        """
        session = None
        try:
            # Inside the try: opening the session can itself fail when the database is down, and
            # that must degrade to "no failure armed" rather than stopping the worker scoring.
            session = self._session_factory()
            mode = consume(session)
            session.commit()
            return mode
        except Exception as exc:
            log.warning("demo_token_check_failed", extra={"error": str(exc)})
            if session is not None:
                session.rollback()
            return None
        finally:
            if session is not None:
                session.close()

    def score(self, conversation: dict[str, Any]) -> ScoringOutcome:
        mode = self._next_failure()
        if mode is None:
            return self._inner.score(conversation)

        metrics.llm_failure_injections_total.labels(mode=mode.value).inc()

        if mode is FailureMode.TIMEOUT:
            # Waits, then raises the same error a real client timeout produces, so it lands in the
            # latency histogram and is classified as transient like any other timeout.
            time.sleep(self._timeout_seconds)
            raise LLMTransientError(
                "simulated provider timeout (demo mode)", ErrorType.TIMEOUT
            )

        if mode is FailureMode.HTTP_500:
            raise LLMTransientError(
                "simulated provider 500 Internal Server Error (demo mode)",
                ErrorType.SERVER_ERROR,
            )

        # MALFORMED: run the bad payload through the real validator, so the failure comes from the
        # contract itself rather than from a hand-thrown exception.
        try:
            ScoringResult.model_validate(MALFORMED_RESPONSE)
        except ValidationError as exc:
            raise LLMInvalidResponseError(
                f"simulated malformed response (demo mode): {exc}"
            ) from exc

        raise AssertionError("unreachable: the malformed payload must fail validation")
