"""Every metric ConvoScore exposes, defined in one place.

**Cardinality is the rule that shapes this file.** A Prometheus time series exists for every
combination of label values, so a label with unbounded values quietly turns one metric into
millions and takes the monitoring system down with it. Nothing here is labelled with a job id, a
conversation id, an object key, or any text derived from a conversation or an exception message.

Every label below is drawn from a fixed set:

* ``source``     - api | s3
* ``status``     - pending | processing | completed | failed
* ``error_type`` - the ErrorType enum
* ``outcome``    - a small fixed set per metric
* ``model``      - the configured model, one or two values in practice
* ``route``      - the FastAPI *route template*, never the resolved path, so
                   ``/api/jobs/{job_id}`` is one series rather than one per job
* ``queue``      - main | dlq

The counters are incremented where the work happens. The gauges are read at scrape time from
PostgreSQL or SQS, because a gauge that is only updated when something happens goes stale exactly
when it matters: a stalled system stops emitting, and the graph flatlines at the last good value
instead of showing the truth.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from app.logging import get_logger

log = get_logger(__name__)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# --------------------------------------------------------------------------------------
# API: the RED signals (rate, errors, duration)
# --------------------------------------------------------------------------------------

http_requests_total = Counter(
    "convoscore_http_requests_total",
    "HTTP requests handled by the API.",
    ["method", "route", "status"],
)

http_request_duration_seconds = Histogram(
    "convoscore_http_request_duration_seconds",
    "API request latency.",
    ["method", "route"],
    # The API never calls the LLM, so anything above a second is a problem rather than normal.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

http_requests_in_progress = Gauge(
    "convoscore_http_requests_in_progress",
    "API requests currently being handled.",
)

# --------------------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------------------

jobs_created_total = Counter(
    "convoscore_jobs_created_total",
    "Scoring jobs accepted, by ingestion path.",
    ["source"],
)

jobs_completed_total = Counter(
    "convoscore_jobs_completed_total",
    "Jobs that produced a validated score.",
    ["source"],
)

jobs_failed_total = Counter(
    "convoscore_jobs_failed_total",
    "Jobs that ended in failure, by cause.",
    ["source", "error_type"],
)

job_processing_duration_seconds = Histogram(
    "convoscore_job_processing_duration_seconds",
    "Time from claiming a job to persisting its result, across one attempt.",
    buckets=(0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0),
)

job_attempts_total = Counter(
    "convoscore_job_attempts_total",
    "Scoring attempts, whatever their outcome.",
    ["outcome"],  # completed | retried | failed | duplicate | lost_claim
)

enqueue_failures_total = Counter(
    "convoscore_enqueue_failures_total",
    "Jobs written to the database that could not be queued. The dual-write gap, made visible.",
    ["source"],
)

# --------------------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------------------

llm_calls_total = Counter(
    "convoscore_llm_calls_total",
    "Calls to the scoring provider, by outcome.",
    ["model", "outcome"],  # success | plus the ErrorType values
)

llm_latency_seconds = Histogram(
    "convoscore_llm_latency_seconds",
    "Provider call latency, successful calls only.",
    ["model"],
    buckets=(0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0),
)

llm_retries_total = Counter(
    "convoscore_llm_retries_total",
    "Attempts that were retried after a transient provider failure.",
    ["error_type"],
)

llm_validation_failures_total = Counter(
    "convoscore_llm_validation_failures_total",
    "Provider responses rejected because they did not satisfy the scoring contract.",
)

llm_tokens_total = Counter(
    "convoscore_llm_tokens_total",
    "Tokens consumed, by direction.",
    ["model", "kind"],  # prompt | completion
)

llm_estimated_cost_usd_total = Counter(
    "convoscore_llm_estimated_cost_usd_total",
    "Estimated spend from the versioned price table. An estimate, not a bill.",
    ["model"],
)

llm_pricing_unknown_total = Counter(
    "convoscore_llm_pricing_unknown_total",
    "Scored jobs whose model is missing from the price table, so cost could not be estimated.",
    ["model"],
)

llm_failure_injections_total = Counter(
    "convoscore_llm_failure_injections_total",
    "Deliberate provider failures injected in demo mode.",
    ["mode"],
)

# --------------------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------------------

ingest_objects_total = Counter(
    "convoscore_ingest_objects_total",
    "Storage objects examined, by outcome.",
    ["outcome"],  # ingested | skipped_duplicate | invalid | failed
)

ingest_poll_duration_seconds = Histogram(
    "convoscore_ingest_poll_duration_seconds",
    "Time to list and process one pass over the incoming prefix.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# --------------------------------------------------------------------------------------
# Worker and queue
# --------------------------------------------------------------------------------------

worker_receive_errors_total = Counter(
    "convoscore_worker_receive_errors_total",
    "Failures talking to the queue, by reason.",
    ["reason"],  # unavailable | database | unexpected
)

worker_loop_iterations_total = Counter(
    "convoscore_worker_loop_iterations_total",
    "Worker loop passes, including idle ones. Flat means the loop has stopped turning.",
)

component_heartbeat_timestamp_seconds = Gauge(
    "convoscore_component_heartbeat_timestamp_seconds",
    "Unix time of the last loop pass. Alert on its age, not its value.",
    ["component"],
)


# --------------------------------------------------------------------------------------
# Scrape-time gauges
# --------------------------------------------------------------------------------------


class CallbackGauges(Collector):
    """Gauges evaluated when Prometheus scrapes, rather than when the application acts.

    Backlog and queue depth are *states*, not events. Publishing them only when something happens
    means a stalled system stops updating them, and the graph holds its last good value exactly
    when an operator needs the truth. Reading them at scrape time makes a stall visible.

    A failing callback logs and yields nothing, so one broken query cannot take the whole
    /metrics endpoint down with it.
    """

    def __init__(self) -> None:
        self._callbacks: list[tuple[str, str, list[str], Callable[[], Any]]] = []

    def register(
        self, name: str, documentation: str, labels: list[str], callback: Callable[[], Any]
    ) -> None:
        self._callbacks.append((name, documentation, labels, callback))

    def collect(self) -> Iterable[GaugeMetricFamily]:
        for name, documentation, labels, callback in self._callbacks:
            family = GaugeMetricFamily(name, documentation, labels=labels)
            try:
                value = callback()
            except Exception as exc:
                log.warning("metric_callback_failed", extra={"metric": name, "error": str(exc)})
                continue

            if labels:
                # Expect {label_value: number} or {(v1, v2): number}
                for key, number in (value or {}).items():
                    label_values = list(key) if isinstance(key, tuple) else [str(key)]
                    family.add_metric(label_values, float(number))
            else:
                family.add_metric([], float(value))
            yield family


def render(registry: CollectorRegistry | None = None) -> bytes:
    """Serialise the default registry in Prometheus text format."""
    if registry is None:
        return generate_latest()
    return generate_latest(registry)
