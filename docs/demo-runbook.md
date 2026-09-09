# Demo runbook

A 20–30 minute walkthrough. Timings assume the system is already up.

Before you start:

```bash
make up          # ~3 minutes cold, ~40 seconds warm
make status      # confirm everything is ready
```

Have these open: the review UI at http://127.0.0.1:8080, Grafana at http://127.0.0.1:3000
(*ConvoScore Overview*), and a terminal.

> If anything was left armed from a rehearsal, clear it first:
> `curl -X DELETE http://127.0.0.1:8000/api/demo/llm-failure`

---

## 1. The problem and the shape of the answer (3 min)

Support teams generate more conversations than anyone can read. ConvoScore scores each one for
sentiment and risk so a lead can triage.

Draw the shape before showing anything:

> A conversation arrives through one of two doors, an API call or a file in object storage. Either
> way it becomes the same durable job in PostgreSQL, and the same worker scores it. Two ingestion
> paths, one pipeline.

The two decisions worth stating up front, because everything else follows from them:

- **Scoring is asynchronous.** The API never calls the model. It validates, writes a job, queues
  the id, and returns `202` in milliseconds. So API latency and availability do not depend on
  OpenAI.
- **PostgreSQL is the source of truth, not the queue.** The message carries only a job id. Losing
  or duplicating a message costs a redelivery, never data.

## 2. The system is healthy (2 min)

Open the **Overview** page. Point at the job counts, the token and cost totals, and the dependency
health panel reading from the live API.

Then `make status` in the terminal, to show the same picture from outside the UI.

## 3. Submit a conversation and watch it move (4 min)

**Submit** page. The example transcript is a billing complaint that ends in a cancellation threat.
Press *Score conversation*.

Narrate what happens:

1. The request returns immediately with a job id and `pending`. Nothing has been scored yet.
2. The status moves to `processing` when a worker claims it, then `completed`.
3. The score appears: negative sentiment, a risk score in the seventies or eighties, and a
   rationale naming the concrete trigger.

Open the full detail. This is where the system earns trust: the transcript, the model that ran,
the prompt and schema versions, token counts, latency, estimated cost, and the attempt count.

> The prompt version is stored per result on purpose. A score is only comparable to another score
> produced by the same prompt and schema, so changing the rubric means bumping a version rather
> than silently mixing results.

## 4. The second door: storage ingestion (3 min)

```bash
make demo-data
```

Six conversations go in through the API, three are uploaded to object storage. Move to
**Conversations** and filter by source.

The point to make:

> These rows came from completely different places, and they are indistinguishable downstream.
> The ingestor only turns objects into jobs; it never scores anything. That is why there is one
> retry policy and one set of metrics rather than two.

Then run `make demo-data` again and refresh. Nothing new appears.

> Polling sees the same objects on every pass. Each object is recorded by bucket, key and etag, so
> re-listing it is free. Without that, every poll would be another job and another charge for a
> conversation already scored. Re-upload *different* content to the same key and it is new work,
> which is what correcting a bad export should do.

## 5. Observability (4 min)

Open Grafana, *ConvoScore Overview*. Walk down the rows rather than reading every panel:

- **Service health** — components up, backlog age, queue depth, dead-letter queue.
- **API RED** — request rate, errors, latency percentiles. Note the API stays in milliseconds
  because it never calls the model.
- **Jobs** — states and throughput.
- **Scoring provider** — calls by outcome, latency, retries.
- **Tokens and cost** — estimated spend, labelled as an estimate everywhere.

Worth saying out loud:

> Backlog age and queue depth are read from the database and from SQS when Prometheus scrapes,
> not pushed when something happens. A gauge that is only updated on activity freezes at its last
> value exactly when the system stalls, which is the moment you need the truth.

And, if the audience is technical:

> Nothing is labelled with a job id. The request metric uses the route template, so
> `/api/jobs/{job_id}` is one series rather than one per job. That is the difference between a
> monitoring system and an outage.

## 6. Break the model on purpose (5 min)

Open the **Demo** tab.

**First, a failure the system absorbs.** Arm one *Provider 500*, then submit a conversation.

Watch the detail page: attempt 1 fails, the job returns to `pending`, attempt 2 succeeds. In
Grafana the *Scoring provider* row shows a `server_error` and the retry counter moving.

> Nothing here touched Prometheus or edited a dashboard. The provider call genuinely failed. The
> error was classified as transient, the retry was scheduled by extending the SQS message's
> visibility timeout, and the graphs moved because the counters moved.

Why the visibility timeout matters, if asked:

> The retry is owned by the queue, not by the worker. If the worker is killed mid-backoff, the
> retry still happens. A `sleep` in the process would lose it.

**Then, a failure it gives up on.** Arm four *Provider timeouts* and submit again.

Three attempts, then `failed` with `max_attempts_exhausted` recorded. Show the detail page: the
reviewer can see exactly how many attempts were made and why it stopped.

> Bounded retries are deliberate. Retrying forever turns one bad conversation into an unbounded
> bill. Permanent errors, a bad key or an unknown model, are never retried at all.

**Disarm before moving on.** The UI shows a red banner while anything is armed, precisely so this
is not forgotten.

## 7. Break the infrastructure (4 min)

```bash
make demo-infra-failure
```

A worker pod is deleted and Kubernetes replaces it. In Grafana, the Kubernetes row shows the gap.

Say why this is a terminal command rather than a button:

> The application has no Kubernetes permissions and no service account token mounted. It cannot
> delete a pod. A demo control that could would be a control an attacker could use, so
> infrastructure failure is something an operator does, from outside.

And what the system did about it:

> A job the dead worker had claimed is redelivered once its visibility timeout expires, and the
> surviving worker picks it up. The claim in the database is what stops both workers scoring it.

## 8. Prove the results are durable (3 min)

```bash
make demo-restart
```

Every application component is restarted and the database pod deleted. The job counts before and
after are identical.

> The results live on a PersistentVolumeClaim, not in any pod. Every process that touched them has
> been replaced and the data is unchanged. Locally that is a volume in kind; in production it is
> RDS with automated backups and point-in-time recovery.

## 9. Local versus production (3 min)

Bring up [architecture-production-aws.md](architecture-production-aws.md) and use the mapping
table. The three deltas worth stating:

1. **Ingestion becomes event-driven.** S3 `ObjectCreated` notifications into a queue, instead of
   polling. Same job, same worker, no listing.
2. **PostgreSQL becomes RDS Multi-AZ.** Running a database in Kubernetes was for demonstrating
   durability on a laptop, not a recommendation.
3. **Workers scale on queue depth and oldest-message age**, not CPU, and bounded by the provider's
   rate limits. Scaling up while the provider returns 429 makes things worse.

## 10. What was left out, and why (2 min)

Close on judgement rather than features. From [DECISIONS.md](../DECISIONS.md):

- **No Redis or cache.** Idempotency and bounded retries already prevent the repeat billing a
  cache would have addressed.
- **No Kafka.** Nothing needs ordering or replay. SQS is the smallest tool that fits.
- **No ArgoCD locally.** One environment and one cluster; a GitOps controller would be ceremony.
- **No transactional outbox.** The gap between the database write and the queue send is real. It
  is surfaced as a `503` with the job marked failed, rather than hidden. The upgrade path is
  written down.

> The single thing I would add first in production is the outbox, because it is the only place
> where a crash can leave a job that nothing will ever process.

---

## If something goes wrong live

| Symptom | Do this |
|---|---|
| Jobs stay `pending` | `make logs COMPONENT=worker`. Usually LocalStack was restarted; the worker re-resolves the queue on its own within about thirty seconds. |
| Everything fails with `client_error` | The OpenAI key is missing or invalid. Check `openai_key_configured` in `/api/health/details`. |
| Jobs fail unexpectedly | Something is still armed. `curl -X DELETE http://127.0.0.1:8000/api/demo/llm-failure` |
| Grafana has no data | Give it one scrape interval (10s). Confirm targets at http://127.0.0.1:9090/targets |
| A pod will not start | `make status`, then `kubectl -n convoscore describe pod <name>` |

**Zero-cost fallback.** If the OpenAI key fails on the day, set `LLM_PROVIDER=fake` in `.env` and
run `make up`. Every part of this runbook still works, including both failure demos; only the
scorer changes.
