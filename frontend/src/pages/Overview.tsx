import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import { RiskBadge, SentimentLabel, SourceBadge, StatusBadge } from "../components/Badges";
import { Card, Empty, ErrorNotice, Loading, Stat } from "../components/Common";
import { formatCost, formatDuration, formatRelative, formatTokens, shortId } from "../lib/format";

export function Overview() {
  const stats = useQuery({
    queryKey: ["stats"],
    queryFn: api.getStats,
    // Work is happening in the background, so the numbers move on their own.
    refetchInterval: 4000,
  });

  const recent = useQuery({
    queryKey: ["conversations", { limit: 8 }],
    queryFn: () => api.listConversations({ limit: 8 }),
    refetchInterval: 4000,
  });

  const health = useQuery({ queryKey: ["health"], queryFn: api.getHealth, refetchInterval: 15000 });

  if (stats.isLoading) return <Loading what="Loading overview" />;
  if (stats.isError) return <ErrorNotice error={stats.error} />;
  if (!stats.data) return null;

  const { by_status: status, ...data } = stats.data;
  const inFlight = status.pending + status.processing;

  return (
    <>
      <h1>Overview</h1>
      <p className="page-intro">
        Conversations arrive through the API or object storage, and are scored asynchronously by
        the worker pool.
      </p>

      <div className="stat-grid">
        <Stat label="Conversations" value={data.total_jobs} sub={sourceSummary(data.by_source)} />
        <Stat
          label="In flight"
          value={inFlight}
          tone={inFlight > 0 ? "warn" : undefined}
          sub={`${status.pending} pending · ${status.processing} processing`}
        />
        <Stat label="Completed" value={status.completed} tone="ok" />
        <Stat
          label="Failed"
          value={status.failed}
          tone={status.failed > 0 ? "danger" : undefined}
          sub={status.failed > 0 ? "see the conversations list" : "none"}
        />
        <Stat
          label="Average risk"
          value={data.average_risk_score === null ? "—" : data.average_risk_score.toFixed(1)}
          sub={data.max_risk_score === null ? undefined : `highest ${data.max_risk_score}`}
        />
      </div>

      <div className="two-col">
        <Card title="LLM usage and estimated cost">
          <dl className="kv">
            <dt>Total tokens</dt>
            <dd>{formatTokens(data.total_tokens)}</dd>
            <dt>Prompt / completion</dt>
            <dd>
              {formatTokens(data.prompt_tokens)} / {formatTokens(data.completion_tokens)}
            </dd>
            <dt>Estimated cost</dt>
            <dd>{formatCost(data.estimated_cost_usd)}</dd>
            <dt>Average latency</dt>
            <dd>{formatDuration(data.average_llm_latency_ms)}</dd>
          </dl>
          <p className="faint" style={{ marginBottom: 0, marginTop: 12 }}>
            Cost is an estimate from a versioned price table, not a bill. Production numbers must be
            reconciled against the provider's invoices.
          </p>
        </Card>

        <Card title="System health">
          {health.data ? (
            <>
              <dl className="kv">
                {health.data.dependencies.map((dependency) => (
                  <ContextRow
                    key={dependency.name}
                    name={dependency.name}
                    healthy={dependency.healthy}
                    detail={dependency.detail}
                  />
                ))}
                <dt>Scoring backend</dt>
                <dd>
                  {String(health.data.config.llm_provider)} ({String(health.data.config.model)})
                </dd>
                <dt>Prompt version</dt>
                <dd>
                  {String(health.data.config.prompt_version)} · schema{" "}
                  {String(health.data.config.schema_version)}
                </dd>
              </dl>
              <p className="faint" style={{ marginBottom: 0, marginTop: 12 }}>
                Sentiment split: {sentimentSummary(data.by_sentiment)}
              </p>
            </>
          ) : (
            <Loading what="Checking dependencies" />
          )}
        </Card>
      </div>

      <div className="card" style={{ marginTop: 18 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Recent conversations</h2>
          <Link to="/conversations">View all →</Link>
        </div>
        {recent.isLoading ? (
          <Loading />
        ) : !recent.data || recent.data.items.length === 0 ? (
          <Empty>
            Nothing scored yet. <Link to="/submit">Submit a conversation</Link> or run{" "}
            <code className="mono">make demo-data</code>.
          </Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Source</th>
                <th>Status</th>
                <th>Sentiment</th>
                <th>Risk</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {recent.data.items.map((item) => (
                <tr key={item.id}>
                  <td className="mono">
                    <Link to={`/conversations/${item.id}`}>{shortId(item.id)}</Link>
                  </td>
                  <td>
                    <SourceBadge source={item.source} />
                  </td>
                  <td>
                    <StatusBadge status={item.status} />
                  </td>
                  <td>
                    <SentimentLabel sentiment={item.sentiment} />
                  </td>
                  <td>
                    <RiskBadge score={item.risk_score} band={item.risk_band} />
                  </td>
                  <td className="dim">{formatRelative(item.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function ContextRow({
  name,
  healthy,
  detail,
}: {
  name: string;
  healthy: boolean;
  detail: string | null;
}) {
  return (
    <>
      <dt>{name}</dt>
      <dd className="row">
        <span className={`dot ${healthy ? "dot-ok" : "dot-bad"}`} />
        <span className="muted" style={{ fontFamily: "inherit", fontSize: 14 }}>
          {detail ?? (healthy ? "healthy" : "unavailable")}
        </span>
      </dd>
    </>
  );
}

function sourceSummary(bySource: Record<string, number>): string {
  const entries = Object.entries(bySource);
  if (entries.length === 0) return "none yet";
  return entries.map(([source, count]) => `${count} ${source === "api" ? "API" : "storage"}`).join(" · ");
}

function sentimentSummary(bySentiment: Record<string, number>): string {
  const entries = Object.entries(bySentiment);
  if (entries.length === 0) return "nothing scored yet";
  return entries.map(([sentiment, count]) => `${count} ${sentiment}`).join(" · ");
}
