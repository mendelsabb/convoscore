# Observability

Every number on the dashboard comes from a metric the application actually emits, or from
Kubernetes itself. Nothing is synthesised for the demo, and the failure-injection controls change
the graphs only by causing real failures in the real processing path.

- **Grafana** — http://127.0.0.1:3000, dashboard *ConvoScore Overview* (anonymous viewer access;
  `admin` / `admin` to edit)
- **Prometheus** — http://127.0.0.1:9090
- **Raw metrics** — http://127.0.0.1:8000/metrics for the API; the worker and ingestor serve
  theirs on port 9100 inside the cluster

## The rule that shapes the metrics: cardinality

A Prometheus time series exists for every distinct combination of label values. A label with
unbounded values therefore turns one metric into millions of series and takes down the monitoring
system with the thing it was meant to watch.

Nothing here is labelled with a job id, a conversation id, an object key, or any text taken from a
conversation or an exception. Every label is drawn from a fixed set:

| Label | Values |
|---|---|
| `source` | `api`, `s3` |
| `status` | `pending`, `processing`, `completed`, `failed` |
| `error_type` | the `ErrorType` enum |
| `outcome` | a small fixed set per metric |
| `model` | the configured model |
| `route` | the FastAPI route *template* |
| `queue` | `main`, `dlq` |

The `route` label is the one that would most easily go wrong. `/api/jobs/{job_id}` is one series;
labelling by resolved path would create one series per job. Anything that never matched a route
collapses to a single `unmatched` series, so a scanner hitting random URLs cannot invent labels.
There is a test for each of these (`backend/tests/api/test_metrics.py`).

## Counters, and gauges read at scrape time

Counters are incremented where the work happens. State is different: backlog age, jobs by status
and queue depth are read from PostgreSQL or SQS *when Prometheus scrapes*.

That distinction matters. A gauge only updated when something happens goes stale exactly when it
is most needed: a stalled system stops emitting, and the graph holds its last good value instead
of showing the truth. Reading at scrape time makes a stall visible as a rising line.

A failing callback logs and yields nothing, so one broken query cannot take the whole `/metrics`
endpoint down.

## What is measured

**API (rate, errors, duration)** — `convoscore_http_requests_total`,
`convoscore_http_request_duration_seconds`, `convoscore_http_requests_in_progress`. Probe and
scrape traffic is excluded; it fires every few seconds and would drown out real usage. The API
never calls the model, so its latency should stay in milliseconds no matter how slow scoring gets.

**Jobs** — `convoscore_jobs_created_total{source}`, `..._completed_total{source}`,
`..._failed_total{source,error_type}`, `convoscore_job_processing_duration_seconds`,
`convoscore_job_attempts_total{outcome}`, `convoscore_enqueue_failures_total{source}`, plus the
scrape-time gauges `convoscore_jobs_by_status{status}` and
`convoscore_oldest_pending_job_age_seconds`.

The attempt outcomes are worth knowing: `completed`, `retried`, `failed`, `duplicate` (an
at-least-once redelivery that was correctly skipped) and `lost_claim` (a worker whose claim
expired, discarding its own result rather than overwriting the takeover worker's).

**Scoring provider** — `convoscore_llm_calls_total{model,outcome}`,
`convoscore_llm_latency_seconds{model}` (successful calls only, so a fast timeout cannot flatter
the numbers), `convoscore_llm_retries_total{error_type}`,
`convoscore_llm_validation_failures_total`.

**Tokens and cost** — `convoscore_llm_tokens_total{model,kind}` and
`convoscore_llm_estimated_cost_usd_total{model}`, computed from the versioned price table in
`backend/app/scoring/pricing.py`. It is an estimate, labelled as one everywhere it appears.
Production billing must be reconciled against the provider's invoices.
`convoscore_llm_pricing_unknown_total` counts jobs whose model is missing from the table, so
spend cannot silently under-report.

**Ingestion** — `convoscore_ingest_objects_total{outcome}` and
`convoscore_ingest_poll_duration_seconds`. Duplicates are the normal case: polling sees the same
objects every pass, and skipping them is what prevents repeat billing.

**Queue** — `convoscore_queue_messages{queue,state}` for both the scoring queue and its
dead-letter queue. Both worker replicas report the same numbers, so the dashboard aggregates with
`max()`, not `sum()`.

**Component health** — `convoscore_component_heartbeat_timestamp_seconds{component}` and
`convoscore_worker_loop_iterations_total`. Alert on the heartbeat's *age*, never its value.
Kubernetes pod readiness, restarts, CPU and memory come from kube-state-metrics and the kubelet's
cAdvisor endpoint.

## Probes, and why liveness checks nothing external

| Process | Liveness | Readiness |
|---|---|---|
| api | `/healthz`, process only | `/readyz`, PostgreSQL reachable |
| worker | heartbeat file newer than 180s | none (no Service) |
| ingestor | heartbeat file newer than 120s | none |
| web | nginx answers `/healthz` | same |
| postgres | `pg_isready` | `pg_isready` |

An OpenAI or database outage must never cause Kubernetes to restart a healthy process. That would
turn a dependency blip into a restart storm and fix nothing; the outage belongs in the metrics,
where someone can see it. Readiness is different: it removes a pod from the load balancer, so it
fails closed on the one dependency without which no endpoint can work.

The worker and ingestor are loops rather than servers, and Kubernetes already restarts a container
whose process exits. What their probes add is detection of a process that is *stuck*: alive, but no
longer going round its loop. The thresholds allow a full long poll plus an LLM timeout plus retry
backoff before declaring a stall.

## Alerts

Six rules ship in `platform/prometheus-values.yaml` and are visible in Prometheus. Alertmanager
routing is deliberately out of scope for a local demo; the `for:` durations are shortened so the
rules can be demonstrated inside a 20-minute walkthrough.

| Alert | Fires when | Production `for:` |
|---|---|---|
| `ConvoScoreAPIDown` | no API instance is scrapeable | 2m |
| `ConvoScoreLLMErrorRateHigh` | over half of provider calls fail | 10m |
| `ConvoScoreJobFailureRateHigh` | over a fifth of jobs fail | 15m |
| `ConvoScoreBacklogAging` | oldest unfinished job exceeds 5 minutes | 10m, threshold tuned to the SLO |
| `ConvoScoreDeadLetterQueueNotEmpty` | any message reaches the DLQ | 5m |
| `ConvoScoreWorkerStalled` | no worker loop pass in 3 minutes | 5m |

**What would page an on-call engineer in production**, as opposed to raising a ticket:

- `ConvoScoreAPIDown` and `ConvoScoreWorkerStalled` are pages. Nothing is being accepted or
  scored, and no amount of waiting fixes either.
- `ConvoScoreBacklogAging` is a page when it breaches the customer-facing SLO, and a ticket below
  it. It is the alert that best predicts a bad morning, because it rises before anything fails.
- `ConvoScoreLLMErrorRateHigh` is a page if it persists, because every job will exhaust its
  retries. A short spike is absorbed by the retry policy and should not wake anyone.
- `ConvoScoreJobFailureRateHigh` and `ConvoScoreDeadLetterQueueNotEmpty` are tickets: they mean
  something is systematically wrong, but the system is still serving.

Alert on symptoms rather than causes. High CPU is not an alert; a growing backlog is.

## Deliberate omissions

- **Alertmanager routing, silences and escalation policies.** Production concerns with no demo
  value.
- **Logs are not aggregated.** They are structured JSON on stdout, which is the right shape for a
  collector; wiring up Loki or CloudWatch would be a second monitoring stack to install.
- **Tracing.** With one synchronous hop and one queue hop, the job id in the logs already tells the
  story. Tracing earns its place once there are several services deep in a call path.
- **kube-prometheus-stack.** The plain chart carries the same signals without an operator or CRDs.
  Production would use the operator stack or Amazon Managed Prometheus, where `ServiceMonitor` and
  `PrometheusRule` objects let each team own its own scrape config and alerts.
