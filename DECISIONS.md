# Architecture Decisions

This document records the architectural decisions behind ConvoScore, the alternatives considered,
and what would change in real production. It is written for a reviewer who wants to understand
*why* the system is shaped this way, not just what it does.

The guiding principle: **a coherent system with a few well-built parts**. Every component below
earns its place in the demo; anything that would only be there to name-drop a technology is
documented as a production consideration instead of being built.

---

## 1. Asynchronous scoring with a durable job record

**Decision.** `POST /api/conversations` validates the input, writes a `pending` row to PostgreSQL,
enqueues the job id, and returns `202 Accepted` with `{job_id, status: "pending"}` in well under a
second. Clients poll `GET /api/jobs/{id}` for `pending → processing → completed | failed`.

**Why.** An LLM call takes one to several seconds, fails intermittently, and is rate limited.
Scoring synchronously inside the HTTP request would tie API latency and availability to OpenAI,
make retries invisible to the client, and make batch ingestion from storage impossible to reason
about. A job record decouples *accepting* work from *doing* it, and gives every conversation a
stable identity for review.

**Consequence.** The client has to poll (or, in production, subscribe to a webhook/SSE). For a human
review tool that is entirely acceptable and makes the state transitions visible in the UI.

## 2. PostgreSQL is the source of truth; the queue is not

**Decision.** All job state, results, attempt counts, errors and LLM metadata live in PostgreSQL.
The SQS message carries only the job id. After a job completes, the message is deleted and nothing
of value is lost.

**Why.** Results must be durable, queryable (list, filter, aggregate) and survive restarts. Queues
are excellent at delivery and terrible at querying. Keeping a single source of truth removes an
entire class of "which system is right?" bugs.

**Production mapping.** In-cluster PostgreSQL with a PersistentVolumeClaim locally (so restart
durability can be demonstrated); Amazon RDS PostgreSQL Multi-AZ in production (see
[docs/architecture-production-aws.md](docs/architecture-production-aws.md)). Running PostgreSQL in
Kubernetes is *not* the production recommendation.

## 3. SQS as the job queue (and why not Kafka, Redis, or the database)

**Decision.** An SQS-compatible queue (LocalStack locally, SQS in AWS) with a dead-letter queue.

**Why SQS.** The workload is "N independent jobs, each processed once, with retries". SQS gives
at-least-once delivery, visibility timeouts, long polling, redrive to a DLQ, and CloudWatch metrics
(queue depth, oldest message age) with zero operational overhead in AWS. It is the smallest tool
that fits.

**Why not Kafka.** Kafka is for ordered, replayable streams with many consumers. Nothing here needs
ordering or replay; the operational cost would be pure overhead.

**Why not Redis.** Redis queues need hand-rolled visibility/ack semantics and add a stateful service
that must also be made durable. SQS already has those semantics.

**Why not "just poll the database".** `SELECT … FOR UPDATE SKIP LOCKED` would work at this scale
and remove a component. It was rejected because the assignment specifically exercises AWS-like
services via Terraform, and because in production the queue is what decouples worker autoscaling
from database load.

## 4. Long-running worker Deployment, not a Kubernetes Job per conversation

**Decision.** Workers are a Kubernetes `Deployment` of long-lived pods that long-poll the queue.
A "job" is an application record. No Kubernetes `Job` or Pod is ever created per conversation.

**Why.** Creating a Pod per LLM call means scheduler latency, image pull, container start and API
server churn for a task that takes two seconds. It also requires the application to hold Kubernetes
RBAC to create workloads, which widens the blast radius. Long-running workers keep a warm HTTP
client, bounded concurrency, and scale horizontally with replicas (locally) or KEDA on queue depth
(production).

The only Kubernetes-level one-shot process in the system is the database migration, which is an
init container on the API deployment.

## 5. Two ingestion paths, one scoring pipeline

**Decision.** The API and the storage ingestor both normalise input into the same conversation
schema, create the same `conversations` row (with `source = api | s3`) and enqueue to the same
queue. There is exactly one worker code path.

**Why.** Two pipelines would mean two sets of retry semantics, two sets of metrics and two places
for bugs. The ingestor exists only to turn objects into jobs.

**Locally: polling.** A single ingestor pod lists the bucket's `incoming/` prefix every few seconds.
Duplicates are prevented by an `ingested_objects` table keyed on `(bucket, key, etag)`; the same
object seen again is skipped, a re-uploaded object with new content (new etag) becomes a new job.
Polling was chosen over emulating S3 event notifications or a Lambda because it is transparent,
needs no extra infrastructure in LocalStack, and behaves identically on real S3.

**Production: events.** `S3 ObjectCreated → SQS ingest queue → ingestor`, which is cheaper and
lower latency than listing. The downstream pipeline is unchanged.

## 6. Idempotency, retries and at-least-once delivery

SQS delivers at least once. Workers crash. OpenAI times out. The design accounts for all of it with
one idea: **every state transition is a single conditional `UPDATE`** on the job row, and the
message is only deleted after the database says the job is finished.

- **Claim.** `UPDATE … SET status='processing', attempt_count = attempt_count + 1,
  locked_until = now() + visibility_timeout WHERE id = :id AND status IN ('pending','processing')
  AND (locked_until IS NULL OR locked_until < now()) RETURNING …`.
  If no row comes back and the job is already `completed`/`failed`, the message is a duplicate and is
  deleted without calling the LLM. If no row comes back because another worker holds the claim, the
  message is left alone and SQS will redeliver it later.
- **Transient failure** (timeout, 429, 5xx, connection error, first schema-invalid response): the
  row goes back to `pending` with `last_error` recorded, and the worker calls
  `ChangeMessageVisibility` with exponential backoff plus jitter so SQS redelivers. After
  `max_attempts` (3) the job is `failed` and the message deleted.
- **Permanent failure** (400/401/403, second schema-invalid response, unknown job id): `failed`
  immediately, message deleted. Permanent errors are never retried.
- **DLQ** with `maxReceiveCount = 5` is a backstop for consumers that crash before they can record
  anything; it is not part of the normal retry path.
- **The OpenAI SDK's built-in retries are disabled** (`max_retries = 0`) so the application owns
  every attempt and each one shows up in the database and in metrics.

**Crash windows.**
- Crash before or during the LLM call: the visibility timeout expires, the claim expires, the job is
  retried. Nothing was billed twice.
- Crash after the LLM answered but before the result was committed: the job is scored again. This is
  the one window where double billing can occur. Closing it would require a distributed transaction
  across OpenAI and PostgreSQL, which does not exist; the window is milliseconds wide and the cost of
  one re-score is a fraction of a cent, so it is accepted and documented.
- Crash after the commit but before the message is deleted: redelivery finds `completed` and just
  deletes the message.

**Why no LLM response cache.** Caching would only help if the *same* conversation were scored more
than once, which the idempotency above already prevents. If re-scoring ever became common, a cache
key would need at least the conversation content hash, model, prompt version and schema version.
Until then a cache would be complexity without a workload.

## 7. The dual-write gap (database insert, then queue send)

Creating a job writes to PostgreSQL and then sends to SQS. If the send fails, a `pending` row would
exist that nothing will ever process.

**Decision.** Keep it simple and visible: on send failure the API marks the row `failed` with
`last_error_type = enqueue_failed`, increments a metric, and returns `503` with the job id. The
client resubmits. No transactional outbox.

**Why.** An outbox table plus a relay process is the correct production answer, but it adds a
component and a background loop for a failure mode that, locally, only occurs when LocalStack is
down. The upgrade path is documented in the production architecture and the limitation is honest.

## 8. Structured LLM output, versioned everything, no clever prompting

**Decision.** The model is asked for a strict JSON schema (`sentiment`, `risk_score`, `rationale`)
via OpenAI structured outputs, and the application re-validates the result regardless. The system
prompt carries the rubric and the rules ("only use what is in the conversation, never invent, keep
the rationale short"); the conversation is passed separately as the user input. Each stored result
records `model`, `prompt_version`, `schema_version` and `pricing_version`.

**Why.** Reviewers need to trust and compare scores. A deterministic schema and documented semantics
matter more than prompt cleverness. Versioning means a prompt change never silently mixes with old
results. `temperature = 0` is used where the model supports it (the gpt-5 family rejects the
parameter, so it is only sent when configured).

**Cost.** Token usage returned by the API is stored per job and an *estimated* cost is computed
from a versioned in-repo price table. Estimates are labelled as such everywhere; production
numbers must be reconciled against the provider's billing.

## 9. One backend image, three commands

`convoscore-backend` runs as three Deployments — `api`, `worker`, `ingestor` — by changing the
container command. One build, one dependency set, one place to patch. The frontend is a separate
nginx image because its build toolchain and runtime are entirely different.

## 10. Local platform: kind + LocalStack 4.14 in-cluster + Terraform

- **kind** because it is the lightest conformant Kubernetes on a laptop and its port mappings give
  stable `127.0.0.1:<port>` URLs without an ingress controller.
- **LocalStack** runs *inside* the kind cluster from a small manifest, so pods reach it by cluster
  DNS, Terraform reaches it through the same port mapping the browser uses, and
  `kind delete cluster` tears down everything. Running it in docker-compose next to the cluster was
  rejected because of `host.docker.internal` fragility and a second lifecycle to manage.
- **Version pin.** LocalStack discontinued its free community image in March 2026; releases from
  `2026.04` require an account token. `localstack/localstack:4.14.0` (February 2026) is the last
  Apache-2.0 tag and is pinned deliberately. IAM policy *enforcement* and state persistence were
  always paid features, so: IAM resources are created and documented but not enforced locally, and
  `make up` is idempotent so it can re-apply Terraform if the LocalStack pod restarts.
- **`SQS_ENDPOINT_STRATEGY=off`.** This is the only LocalStack queue-URL format the AWS Terraform
  provider accepts; it validates the URL shape and rejects the `path` strategy outright. The URL's
  hostname is never resolved by the application, because boto3 sends every SQS request to the
  configured endpoint and carries the queue URL in the request body.
- **Terraform** provisions exactly what the application uses — the S3 bucket, the SQS queue and its
  DLQ, and per-component IAM policies/roles — and its outputs are fed into the Helm release. There is
  no decorative Terraform.

## 11. Secrets

- Locally: `.env` (gitignored) → Kubernetes Secrets created by `make up`. The Helm chart references
  secrets by name and never contains values; `helm template` output is safe to paste anywhere.
- The PostgreSQL password is generated once and stored only in the cluster.
- Images contain no secrets. Logs redact settings.
- Production: AWS Secrets Manager + External Secrets Operator, EKS Pod Identity per component,
  rotation, CloudTrail audit, no long-lived AWS keys. See the production document.

## 12. Least privilege

- Terraform defines one IAM policy per component with only the actions it needs (API: send to
  the scoring queue; worker: receive/delete/change-visibility/get-attributes; ingestor: list/get on
  the `incoming/` prefix and send to the queue) attached to roles shaped for EKS Pod Identity.
- In Kubernetes every component has its own ServiceAccount with no token mounted and no RBAC
  bindings. The application cannot list, delete or create anything in the cluster. Infrastructure
  failure demos are operator commands.

## 13. Observability: real metrics, one strong dashboard

**Decision.** Every process exposes `/metrics`; Prometheus (the plain `prometheus-community`
chart with kube-state-metrics) scrapes pods by annotation; Grafana loads one dashboard,
"ConvoScore Overview", from a ConfigMap shipped in the application chart.

**Why the plain chart and not kube-prometheus-stack.** The operator stack brings CRDs, an
operator, ~100 default rules and node-exporter (which needs a workaround on Docker Desktop) for a
single-cluster laptop demo. The plain chart delivers the same application metrics and Kubernetes
pod health with a fraction of the footprint. In production the operator stack (ServiceMonitor,
PrometheusRule) or Amazon Managed Prometheus/Grafana is the natural choice.

**What is measured, in priority order:** API RED (rate, errors, duration) → job throughput and
status → LLM latency, error and retry counts → token usage and estimated cost → ingestion
success/failure/duplicates → pod health and restarts. Nothing on the dashboard is synthetic: every
panel is a Prometheus query over metrics the application or Kubernetes actually emits.

**Probes.** Liveness never depends on external dependencies: an OpenAI outage must not make
Kubernetes restart a healthy process, which would turn a dependency blip into a restart storm and
fix nothing. API readiness checks only the database, because no endpoint can do useful work
without it; the queue and object store are reported through `/api/health/details` instead, since
they degrade specific features rather than making the API useless.

The worker and ingestor are loops rather than servers, and Kubernetes already restarts a container
whose process exits. What a probe adds there is detection of a process that is *stuck*: alive, but
no longer going round its loop. Each pass touches a heartbeat file and the probe checks its age,
with a threshold that allows a full long-poll plus an LLM timeout plus retry backoff without a
false positive.

## 14. Failure injection that is real

**LLM failures** are armed through a demo-only API (`DEMO_MODE=true`) and stored as
counted "failure tokens" in PostgreSQL so they work deterministically with several worker pods.
Before each LLM call the worker's provider wrapper consumes a token and, if one was armed, fails at
the provider boundary: a simulated timeout (after a short delay so it registers in the latency
histogram), a simulated 5xx, or a malformed response that the real validator rejects. Everything
downstream — error classification, visibility-timeout backoff, attempt counting, state transitions,
metrics, logs — is the production code path. Prometheus and Grafana change because they are
scraping real counters, not because the demo touched them.

**Infrastructure failures** are `make demo-infra-failure`, an operator command that deletes a
worker (or API, or PostgreSQL) pod and lets Kubernetes recreate it. The UI has no such button by
design (see §12). Restart durability is shown by restarting every component and re-reading results.

## 15. Lightweight React UI behind the API

A small Vite + React app served by nginx, which also reverse-proxies `/api` to the API service.

**Why the proxy.** The app only ever requests relative paths, so there is no build-time API URL to
configure per environment and no CORS policy to maintain. More importantly it means the browser
has exactly one way to reach anything: it cannot talk to PostgreSQL, SQS, S3 or OpenAI, and it can
only do what the API already allows.

**What is deliberately absent.** No state management library beyond React Query, no component
framework, no design system: four screens and a table do not justify them. Filters live in the URL
rather than in component state, so a reviewer can share "everything above 75".

The screens are Overview, Submit, Conversations and Detail, plus the demo controls added in §14.
Screens that show work in progress poll, because the pipeline is asynchronous and the state
transitions are the thing worth seeing.

## 16. Delivery: Helm now, ArgoCD in production, no ApplicationSet

Helm is the deployment interface locally and in production. ArgoCD (one `Application` per
environment) is the production GitOps layer and is documented, not run locally — it would add a
controller and a Git round-trip to a demo that has neither a second environment nor a second
cluster. `ApplicationSet` is only justified when applications must be *generated* across many
clusters or environments; it is not.

## 17. Data sensitivity

Raw conversation text is stored because a human reviewer must see it. The raw LLM response is not
stored — only the validated result and execution metadata — because it adds no review value and is
one more copy of customer content. Logs contain job ids, statuses, error types and durations, never
message content or secrets. In production: encryption at rest (RDS/S3 KMS), retention and deletion
policies, PII redaction before the LLM call where required, and access control on the review UI.

## 18. Migrations

Alembic migrations run in an init container of the API deployment (with a PostgreSQL advisory lock)
because on a fresh `helm install` a pre-install hook would run before the in-chart PostgreSQL
exists. Worker and ingestor wait for the schema before starting their loops. In production, with
RDS provisioned separately, migrations run as an explicit pipeline step before rollout.

---

## Known limitations (intentional, for this assignment)

- Single API replica; no HPA/KEDA, PodDisruptionBudgets, NetworkPolicies or ingress controller.
- LocalStack does not enforce IAM and loses state if its pod restarts (mitigated by idempotent
  `make up`).
- No transactional outbox for job creation (§7).
- Alerting is limited to a handful of Prometheus rules without Alertmanager routing.
- Estimated cost only; no reconciliation with provider billing.
- No authentication on the API or UI; it is reachable only on `127.0.0.1`.
- No re-scoring of an existing conversation under a new prompt version (one job per conversation).

## What changes at scale

- Worker autoscaling on SQS depth and oldest-message age (KEDA), bounded by the provider's rate
  limits — never scale up while the provider returns 429; Karpenter supplies node capacity.
- Event-driven S3 ingestion instead of polling; ingestion becomes another queue consumer.
- Transactional outbox for job creation; DLQ alarms and redrive tooling.
- Partitioning/archival of the `conversations` table, retention policies, read replicas for the
  review UI.
- Per-tenant cost attribution and provider billing reconciliation; possibly a response cache keyed
  on content hash + model + prompt version + schema version if re-scoring becomes routine.
