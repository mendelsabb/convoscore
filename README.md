# ConvoScore

ConvoScore is a small LLM-powered service that scores customer-support conversations.

A conversation enters through one of two doors: a direct API call, or a JSON object dropped into
S3-compatible storage. Either way it becomes the same durable job, is scored asynchronously by an
OpenAI model, and the structured result is persisted in PostgreSQL where a human can review it in a
web UI. Every stage of the pipeline is observable in Prometheus and Grafana, and failures can be
induced on purpose to watch the signals move.

Each result contains:

| Field | Meaning |
|---|---|
| `sentiment` | `positive`, `neutral` or `negative` — the customer's overall tone |
| `risk_score` | integer 0–100 — how likely the account needs intervention (churn, escalation, legal/safety exposure) |
| `rationale` | one or two reviewable sentences naming the concrete trigger in the conversation |

The rubric is defined in [docs/scoring-rubric.md](docs/scoring-rubric.md).

## How it works

```
 Direct API                          S3-compatible storage
 POST /api/conversations             incoming/*.json
        │                                   │
        ▼                                   ▼
      API ──── create job ────►  PostgreSQL  ◄──── Ingestor (polls, de-duplicates)
        │                          (source of truth)         │
        └──────── enqueue job_id ──►  SQS queue  ◄───────────┘
                                          │
                                          ▼
                              Worker Deployment (long-running)
                                claim job → OpenAI → validate → persist result
                                          │
                                          ▼
                     PostgreSQL (completed / failed, tokens, latency, estimated cost)
                                          │
                                          ▼
                      React review UI ──► API ──► (never talks to DB/SQS/S3/OpenAI directly)
```

Two ingestion paths, one scoring pipeline. Kubernetes runs long-lived services; a "job" is an
application record, not a Kubernetes Job.

Job lifecycle: `pending → processing → completed | failed`, with bounded retries driven by the SQS
visibility timeout and idempotent state transitions in PostgreSQL. See
[docs/architecture-local.md](docs/architecture-local.md).

## Status

Implementation in progress. Milestones:

1. ✅ Architecture, decisions and rubric documented
2. ✅ FastAPI backend with PostgreSQL persistence and migrations
3. ✅ Async scoring pipeline: SQS worker + structured OpenAI scoring
4. ✅ S3 ingestion into the same pipeline
5. ✅ Deployed to Kubernetes: Docker, kind, Helm, LocalStack, Terraform, `make up` / `make down`
6. ✅ React review UI at http://127.0.0.1:8080
7. ✅ Prometheus metrics and the *ConvoScore Overview* Grafana dashboard
8. Failure injection, demo tooling, CI, final documentation

Working today: `make up` builds both images, creates a kind cluster, deploys LocalStack,
provisions the bucket, queue and IAM policies with Terraform, and installs the application.
Conversations submitted through the API or dropped into storage are scored by the worker, stored
durably, reviewable in the web UI, and visible in Grafana. `make down` removes all of it. Covered
by 194 automated tests.

Sample conversations live in [demo/fixtures](demo/fixtures): six for the API path and three for
storage ingestion, covering satisfied, neutral, frustrated, churn-threat, escalation and
data-privacy cases. Their scores are never hardcoded; they go through the model like anything else.

Still to come (milestone 8): deliberate failure injection, the demo runbook, a CI workflow, and
the production AWS architecture document.

Set `LLM_PROVIDER=fake` to exercise the whole pipeline deterministically with no OpenAI spend.

## Prerequisites

Everything runs locally at zero cost except the OpenAI API calls.

| Tool | Notes |
|---|---|
| Docker Desktop (or another Docker engine) | allocate at least 6 GB of memory; the stack uses roughly 3 GB |
| `kind` | `brew install kind` |
| `kubectl`, `helm` (3 or 4) | `brew install kubectl helm` |
| `terraform` ≥ 1.5 | `brew install terraform` |
| `make`, `bash`, `curl` | already present on macOS/Linux |
| `aws` CLI (optional) | only for uploading S3 demo fixtures; a dockerised fallback is used otherwise |

You do not need Python or Node on the host: images are built in Docker. For local development
and tests, `uv` (Python) and Node 20+ are used.

## Quick start

```bash
cp .env.example .env          # put your OpenAI key in .env (gitignored, never committed)
make up                       # cluster + LocalStack + Terraform + app, prints URLs
make demo-data                # submits sample conversations via the API and uploads some to S3
make status                   # pods, URLs, dependency health, job counts
make down                     # tears everything down
```

A cold `make up` takes roughly three minutes, most of which is pulling the kind node and
LocalStack images. Re-running it takes about thirty seconds and preserves your data, so it is also
the way to deploy a code change. `make help` lists every target.

To run the whole system without spending anything on OpenAI, set `LLM_PROVIDER=fake` in `.env`.
The pipeline, the failure behaviour and the metrics are identical; only the scorer changes.

## URLs (once `make up` finishes)

| What | URL |
|---|---|
| Review UI | http://127.0.0.1:8080 |
| API + Swagger | http://127.0.0.1:8000/docs |
| Grafana (dashboard "ConvoScore Overview") | http://127.0.0.1:3000 |
| Prometheus | http://127.0.0.1:9090 |
| LocalStack (S3/SQS emulation) | http://127.0.0.1:4566 |

The review UI has four screens:

- **Overview** — job counts by status, average risk, token usage and estimated cost, dependency
  health, and the most recent conversations.
- **Submit** — paste a transcript and watch it move through `pending → processing → completed`.
- **Conversations** — every scored conversation from both ingestion paths, filterable by status,
  source, sentiment and risk band. Filters live in the URL so a view can be shared.
- **Detail** — the transcript, the score and rationale, the model and prompt version, token usage,
  latency, estimated cost, attempt count and timeline.

## Repository layout

```
backend/        FastAPI API, worker and ingestor (one image, three commands), Alembic migrations, tests
frontend/       React review UI (Vite + TypeScript), served by nginx which also proxies /api
helm/convoscore Helm chart for the application (api, worker, ingestor, web, postgres + PVC)
terraform/local Terraform for the AWS-like resources used locally (S3, SQS + DLQ, IAM) on LocalStack
platform/       kind config, LocalStack manifest, Prometheus/Grafana values — installed by make up
demo/fixtures   synthetic support conversations for the demo (API and S3 variants)
scripts/        the shell behind every make target
docs/           architecture (local and production AWS), rubric, observability, demo runbook
```

## Documentation

- [DECISIONS.md](DECISIONS.md) — why the system is shaped this way, tradeoffs, known limitations
- [docs/architecture-local.md](docs/architecture-local.md) — what actually runs on your machine
- [docs/scoring-rubric.md](docs/scoring-rubric.md) — what sentiment and risk_score mean
- `docs/architecture-production-aws.md` — what changes in real AWS (milestone 8)
- [docs/observability.md](docs/observability.md) — metric catalogue, dashboard, alerts, probe semantics
- `docs/demo-runbook.md` — the live demo script (milestone 8)

## Security notes

- The OpenAI key and database password live only in a gitignored `.env` and in Kubernetes Secrets
  created at setup time. They are never baked into images, Helm values or Git.
- The application has no Kubernetes RBAC permissions; infrastructure failure demos are operator
  commands, not UI buttons.
- Raw conversation text is stored for human review; logs never contain it.
