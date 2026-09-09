# Production architecture on AWS

What ConvoScore would look like running for real, and how it differs from what runs locally.

Nothing in this document is deployed. It is a design, written to be argued with rather than
admired, and it is deliberately not a list of every AWS service that could be involved. Each
component below is here because a requirement asks for it.

The application code does not change. The local system already talks to S3, SQS and PostgreSQL
through their real APIs; production swaps the endpoints, the identities and the operational
guarantees underneath them.

---

## Local to production mapping

| Local | Production | Why it changes |
|---|---|---|
| kind, one node | EKS across three availability zones | A single node is a single point of failure |
| LocalStack SQS | Amazon SQS + DLQ | Same API; managed durability and CloudWatch metrics |
| LocalStack S3 | Amazon S3 with event notifications | Same API; events remove the need to poll |
| PostgreSQL StatefulSet + PVC | RDS PostgreSQL Multi-AZ | Backups, point-in-time recovery, failover. **In-cluster PostgreSQL was for demonstrating durability, never a recommendation** |
| Images loaded with `kind load` | ECR, scan on push, immutable tags | Provenance and vulnerability scanning |
| NodePort on 127.0.0.1 | ALB via the AWS Load Balancer Controller, TLS from ACM | Public traffic needs TLS, health checks and a stable name |
| Kubernetes Secret from `.env` | Secrets Manager via External Secrets Operator | Rotation, audit, no secret on anyone's laptop |
| Dummy `test`/`test` credentials | EKS Pod Identity, one role per component | No long-lived keys anywhere |
| IAM policies created but unenforced | The same policy documents, enforced | LocalStack cannot enforce IAM; AWS does |
| Prometheus + Grafana charts | Prometheus Operator or Amazon Managed Prometheus + Grafana | Multi-tenant, retained, highly available |
| `make up` | Terraform for infrastructure, ArgoCD for the application | Reviewable, auditable, repeatable |
| Two worker replicas, fixed | KEDA on queue depth and oldest-message age | Work arrives in bursts |

---

## Shape of the system

```
                            Route53  →  ACM certificate
                               │
                    ┌──────────▼───────────┐
                    │  Application Load    │   public subnets, 3 AZs
                    │  Balancer            │
                    └──────────┬───────────┘
                               │
  ┌────────────────────────────▼──────────────────────────────┐
  │  EKS, private subnets, 3 AZs                              │
  │                                                            │
  │   web (nginx)        api (HPA)         ingestor            │
  │        │                 │                  ▲              │
  │        └────► /api ──────┤                  │              │
  │                          │                  │              │
  │                     worker (KEDA on queue depth)           │
  │                          │                  │              │
  └──────────┬───────────────┼──────────────────┼──────────────┘
             │               │                  │
      NAT Gateway       RDS PostgreSQL      SQS ingest queue
             │           Multi-AZ                ▲
             ▼                                   │
         OpenAI                    S3 ObjectCreated notification
                                             ▲
                                    S3 conversations bucket
                                             ▲
                          SQS scoring queue ─┴─ + dead-letter queue
```

VPC endpoints for S3 (gateway), and for SQS, ECR, Secrets Manager and CloudWatch Logs (interface),
so traffic to AWS services never leaves the VPC. The NAT Gateway exists for one reason: OpenAI is
on the public internet.

## Networking

Three availability zones, public subnets for the load balancer and NAT, private subnets for
everything else. Nodes and RDS have no public address.

Security groups are chained rather than open: the ALB accepts 443 from the internet, nodes accept
traffic only from the ALB's security group, and RDS accepts 5432 only from the nodes'. Nothing is
reachable by CIDR alone.

Two AZs would satisfy Multi-AZ RDS and survive one failure. Three is the default because the cost
difference is a NAT Gateway and it removes the awkward case where losing one AZ leaves no quorum
for anything that needs one.

## Ingestion becomes event-driven

This is the most substantial change from local.

```
S3 ObjectCreated → SQS ingest queue → ingestor → conversations row + scoring queue
```

Locally the ingestor lists the `incoming/` prefix on a timer. That is transparent and needs no
extra infrastructure, which is exactly right for a demo. In production it is wrong in three ways:
latency is bounded below by the poll interval, cost grows with the number of objects rather than
the number of *new* objects, and listing a bucket with millions of keys is slow regardless of how
few are new.

An S3 notification to SQS fixes all three. The ingestor becomes another queue consumer, which is
a shape the codebase already has.

**Notifications go to SQS, not directly to the ingestor.** A queue absorbs bursts, retries on its
own, and gives a dead-letter queue for objects that cannot be processed. A Lambda would work too,
and would be a reasonable choice if ingestion were the only workload; here it would mean a second
runtime, a second deployment mechanism and a second place for the conversation schema to live, for
a component that already exists.

**Deduplication stays.** S3 notifications are also at-least-once, and a redrive can replay them.
The `ingested_objects` table keyed on `(bucket, key, etag)` is what makes replay safe, and it is
unchanged.

## Data

**RDS PostgreSQL Multi-AZ**, encrypted with KMS, automated backups with point-in-time recovery,
and a read replica once the review UI's query load justifies it. Credentials in Secrets Manager
with rotation; the application already composes its connection string from parts, so rotation is a
credential change rather than a code change.

Migrations move out of the pod. Locally they run as an init container because there is no pipeline;
in production they run as an explicit deployment step against RDS before the new version rolls out,
so a failed migration blocks the deploy rather than crash-looping a pod.

**Retention and privacy** matter more here than anywhere else in this document. Conversations are
customer data:

- Encrypt at rest (RDS and S3 with KMS) and in transit.
- Set a retention period and enforce it. Scores stay useful long after transcripts should have
  been deleted, so the transcript column is the one to expire, not the row.
- Redact obvious identifiers before the model call if the policy requires it, accepting that this
  costs some scoring quality.
- Access to the review UI is authenticated and audited. Locally there is no auth at all, because
  it is bound to 127.0.0.1.
- The raw model response is already not stored. The validated result is enough for review, and it
  is one less copy of customer content.

## Identity and secrets

**EKS Pod Identity**, one role per component, with the policy documents Terraform already defines
in `terraform/local/iam.tf`: the API may send to the scoring queue, the worker may receive, delete
and change visibility, the ingestor may read the `incoming/` prefix and send. Nothing may delete an
object or a queue.

LocalStack creates these roles but cannot enforce them, which is stated plainly rather than
glossed over. The point of writing them now is that production applies the same documents.

**Secrets Manager** holds the OpenAI key and the database credentials, synchronised into
Kubernetes by the External Secrets Operator. No static AWS access keys exist anywhere. CloudTrail
records every secret read, which is the audit trail a laptop `.env` cannot provide.

## Scaling

**Workers scale on queue depth and oldest-message age, not CPU.** A worker waiting on a network
call uses almost no CPU, so a CPU-based autoscaler would sit idle while the backlog grew. KEDA
reads the SQS metrics directly.

**The ceiling is the provider's rate limit, not the cluster's capacity.** Scaling up while OpenAI
returns 429 makes the situation worse: more concurrency, more throttling, more retries, more spend,
no more throughput. `maxReplicaCount` is set from the account's rate limit, and the retry policy
already backs off with jitter so retries do not synchronise into bursts.

**Karpenter** provides node capacity. Two layers, with distinct jobs: KEDA decides how many worker
pods should exist, Karpenter finds somewhere to put them.

The API scales on CPU and request rate, which is conventional because it does conventional work.

**Availability.** Topology spread constraints across AZs so one zone failing cannot take every
replica of a component. PodDisruptionBudgets so a node drain cannot evict every worker at once.
Neither is in the local chart: on one node they would be untestable decoration.

## Observability

Prometheus Operator on the cluster, or Amazon Managed Prometheus if the retention and availability
requirements justify the managed service, with Grafana or Amazon Managed Grafana. The application
metrics are unchanged; the dashboard JSON travels with the chart as it already does.

CloudWatch covers what the application cannot see about itself: SQS queue depth and message age
independently of the workers, RDS connections and replication lag, ALB status codes and target
health. Where a signal exists in both places, alert on CloudWatch: it keeps reporting when the
application has stopped.

Alertmanager routes to PagerDuty. What pages and what does not is set out in
[observability.md](observability.md); the short version is that pages go out for "nothing is being
scored" and "the backlog has breached the SLO", while everything else raises a ticket.

Logs are already structured JSON on stdout, which is what a collector wants. CloudWatch Logs or
Loki, with the job id as the correlation key.

Tracing is not proposed. With one synchronous hop and one queue hop, the job id in the logs already
tells the whole story. Tracing earns its place when a request crosses several services.

## Delivery

Terraform for infrastructure with remote state in S3 and DynamoDB locking, one workspace per
environment. The resource definitions in `terraform/local` are the same shape; the provider block
and the backend differ.

ArgoCD for the application: one `Application` per environment, tracking a Git revision. Not
`ApplicationSet`, which generates Applications across many clusters or environments and would be
solving a problem this system does not have. If ConvoScore later ran per-region or per-tenant, that
is when a generator earns its place.

CI is already in this repository and validates what it ships. CD would add: build and push to ECR
on merge, run migrations against RDS, update the image tag in the environment repository, and let
ArgoCD roll it out.

## Cost

The estimate to watch is not the infrastructure, it is the model. At production volume the
scoring spend dwarfs the cluster, which is why token usage and estimated cost are first-class
metrics rather than an afterthought.

The estimate comes from a versioned price table and is labelled an estimate everywhere. In
production it must be reconciled against Cost Explorer and the provider's invoices, because the
table cannot know about negotiated rates, batch discounts or cached-input pricing. Tag every
resource for cost allocation, and attribute scoring spend per tenant if the product needs it.

Obvious levers, roughly in order of value: a smaller model where the rubric tolerates it, shorter
prompts, and batching where latency allows. A response cache is not on this list, because
idempotency already prevents the repeat scoring it would have avoided.

---

## What would change first

In order, if this were becoming a real service:

1. **A transactional outbox for job creation.** The only place where a crash can leave a job that
   nothing will ever process. Today it is surfaced as a `503` and a failed row, which is honest but
   is not a fix.
2. **Authentication on the API and the review UI.** Locally it is bound to 127.0.0.1, which is why
   there is none.
3. **Event-driven ingestion**, as described above.
4. **Retention and PII policy on stored transcripts**, before the volume of stored customer data
   makes it a bigger decision than it is today.
5. **A second reviewer signal.** Letting a human agree or disagree with a score is what would turn
   this from a scoring service into something that improves.
