"""Demo-only endpoints for arming deliberate scoring failures.

This router is mounted only when ``DEMO_MODE`` is true, so the endpoints simply do not exist in a
normal deployment. Note what they can and cannot do: they arm a counter that makes real scoring
calls fail. They cannot write to Prometheus, edit a dashboard, or touch Kubernetes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body

from app import demo
from app.api.deps import DbSession
from app.models import FailureMode
from app.schemas import DemoArmRequest, DemoStateResponse

router = APIRouter(tags=["demo"], prefix="/demo")


def _state(session) -> DemoStateResponse:  # type: ignore[no-untyped-def]
    armed = demo.state(session)
    return DemoStateResponse(
        armed=armed.modes,
        total_armed=armed.total,
        modes=[mode.value for mode in FailureMode],
    )


@router.get(
    "/state",
    response_model=DemoStateResponse,
    summary="How many deliberate failures are currently armed",
)
def get_state(session: DbSession) -> DemoStateResponse:
    return _state(session)


@router.post(
    "/llm-failure",
    response_model=DemoStateResponse,
    summary="Arm deliberate scoring failures",
)
def arm_failure(
    session: DbSession,
    payload: Annotated[DemoArmRequest, Body()],
) -> DemoStateResponse:
    """Make the next `count` scoring calls fail in the given way.

    The failure happens inside the worker's provider call. Everything downstream, including
    retries, attempt counts, state transitions and metrics, is the ordinary code path.
    """
    demo.arm(session, FailureMode(payload.mode), payload.count)
    session.flush()
    return _state(session)


@router.delete(
    "/llm-failure",
    response_model=DemoStateResponse,
    summary="Disarm all deliberate failures",
)
def clear_failures(session: DbSession) -> DemoStateResponse:
    demo.clear(session)
    session.flush()
    return _state(session)
