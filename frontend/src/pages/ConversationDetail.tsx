import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import { RiskBadge, SentimentLabel, SourceBadge, StatusBadge } from "../components/Badges";
import { Card, ErrorNotice, Loading } from "../components/Common";
import { formatCost, formatDuration, formatTimestamp, formatTokens } from "../lib/format";

export function ConversationDetailPage() {
  const { id } = useParams<{ id: string }>();

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["conversation", id],
    queryFn: () => api.getConversation(id as string),
    enabled: Boolean(id),
    // Keep polling while the job is still moving, so opening a pending job is not a dead end.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "completed" || status === "failed" ? false : 1500;
    },
  });

  if (isLoading) return <Loading what="Loading conversation" />;
  if (isError) return <ErrorNotice error={error} />;
  if (!data) return null;

  return (
    <>
      <p style={{ marginTop: 0 }}>
        <Link to="/conversations">← All conversations</Link>
      </p>

      <div className="spread" style={{ marginBottom: 18 }}>
        <div>
          <h1 style={{ marginBottom: 6 }}>Conversation</h1>
          <div className="row">
            <span className="mono faint">{data.id}</span>
            <SourceBadge source={data.source} />
            <StatusBadge status={data.status} />
          </div>
        </div>
      </div>

      {data.status === "failed" ? (
        <div className="notice notice-error">
          <strong>Scoring failed: {data.error?.type ?? "unknown error"}</strong>
          {data.error?.message ? <div style={{ marginTop: 6 }}>{data.error.message}</div> : null}
          <div className="faint" style={{ marginTop: 6 }}>
            {data.attempt_count} of {data.max_attempts} attempts used.
          </div>
        </div>
      ) : null}

      {data.status === "pending" || data.status === "processing" ? (
        <div className="notice notice-info">
          <span className="spin" /> This conversation is still being scored. Attempt{" "}
          {Math.max(1, data.attempt_count)} of {data.max_attempts}.
        </div>
      ) : null}

      {data.result ? (
        <Card title="Score">
          <div className="stat-grid" style={{ marginBottom: 4 }}>
            <div className="stat">
              <div className="label">Sentiment</div>
              <div className="value" style={{ fontSize: 22 }}>
                <SentimentLabel sentiment={data.result.sentiment} />
              </div>
            </div>
            <div className="stat">
              <div className="label">Risk score</div>
              <div className="value" style={{ fontSize: 22 }}>
                <RiskBadge score={data.result.risk_score} band={data.result.risk_band} />
              </div>
              <div className="sub">{data.result.risk_band_label}</div>
            </div>
          </div>
          <p className="rationale" style={{ marginTop: 14 }}>
            {data.result.rationale}
          </p>
        </Card>
      ) : null}

      <div className="two-col" style={{ marginTop: 18 }}>
        <Card title="Conversation">
          <div className="transcript">
            {data.conversation.messages.map((message, index) => (
              <div key={index} className={`message message-${message.role}`}>
                <div className="who">{message.role}</div>
                {message.content}
              </div>
            ))}
          </div>
          {Object.keys(data.conversation.metadata ?? {}).length > 0 ? (
            <p className="faint" style={{ marginTop: 14, marginBottom: 0 }}>
              Metadata: <span className="mono">{JSON.stringify(data.conversation.metadata)}</span>
              <br />
              Metadata is routing information and is not sent to the model.
            </p>
          ) : null}
        </Card>

        <div>
          <Card title="Model execution">
            <dl className="kv">
              <dt>Model</dt>
              <dd>{data.llm.model ?? "—"}</dd>
              <dt>Prompt version</dt>
              <dd>{data.llm.prompt_version ?? "—"}</dd>
              <dt>Schema version</dt>
              <dd>{data.llm.schema_version ?? "—"}</dd>
              <dt>Latency</dt>
              <dd>{formatDuration(data.llm.llm_latency_ms)}</dd>
              <dt>Attempts</dt>
              <dd>
                {data.attempt_count} of {data.max_attempts}
              </dd>
            </dl>
          </Card>

          <Card title="Tokens and estimated cost">
            <dl className="kv">
              <dt>Prompt tokens</dt>
              <dd>{formatTokens(data.llm.prompt_tokens)}</dd>
              <dt>Completion tokens</dt>
              <dd>{formatTokens(data.llm.completion_tokens)}</dd>
              <dt>Total tokens</dt>
              <dd>{formatTokens(data.llm.total_tokens)}</dd>
              <dt>Estimated cost</dt>
              <dd>{formatCost(data.llm.estimated_cost_usd)}</dd>
              <dt>Price table</dt>
              <dd>{data.llm.pricing_version ?? "—"}</dd>
            </dl>
          </Card>

          <Card title="Timeline">
            <dl className="kv">
              <dt>Source</dt>
              <dd>{data.source_ref ?? "direct API submission"}</dd>
              <dt>Created</dt>
              <dd>{formatTimestamp(data.created_at)}</dd>
              <dt>Queued</dt>
              <dd>{formatTimestamp(data.enqueued_at)}</dd>
              <dt>Started</dt>
              <dd>{formatTimestamp(data.started_at)}</dd>
              <dt>Completed</dt>
              <dd>{formatTimestamp(data.completed_at)}</dd>
            </dl>
          </Card>
        </div>
      </div>
    </>
  );
}
