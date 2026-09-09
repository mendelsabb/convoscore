import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import type { ConversationFilters, JobStatus, Sentiment, Source } from "../api/types";
import { RiskBadge, SentimentLabel, SourceBadge, StatusBadge } from "../components/Badges";
import { Empty, ErrorNotice, Loading } from "../components/Common";
import { formatRelative, shortId } from "../lib/format";

const PAGE_SIZE = 25;

export function Conversations() {
  // Filters live in the URL so a reviewer can share "show me everything above 75".
  const [params, setParams] = useSearchParams();

  const filters: ConversationFilters = {
    status: (params.get("status") as JobStatus) || undefined,
    source: (params.get("source") as Source) || undefined,
    sentiment: (params.get("sentiment") as Sentiment) || undefined,
    min_risk: params.get("min_risk") ? Number(params.get("min_risk")) : undefined,
    limit: PAGE_SIZE,
    offset: Number(params.get("offset") ?? 0),
  };

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["conversations", filters],
    queryFn: () => api.listConversations(filters),
    refetchInterval: 5000,
  });

  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    next.delete("offset"); // a new filter means a new first page
    setParams(next);
  }

  function setOffset(offset: number) {
    const next = new URLSearchParams(params);
    if (offset > 0) next.set("offset", String(offset));
    else next.delete("offset");
    setParams(next);
  }

  const offset = filters.offset ?? 0;
  const hasFilters = Boolean(filters.status || filters.source || filters.sentiment || filters.min_risk);

  return (
    <>
      <h1>Conversations</h1>
      <p className="page-intro">
        Everything scored, from both ingestion paths. Click an ID to inspect the transcript and the
        full result.
      </p>

      <div className="filters">
        <div>
          <label htmlFor="status">Status</label>
          <select id="status" value={params.get("status") ?? ""} onChange={(e) => setFilter("status", e.target.value)}>
            <option value="">All</option>
            <option value="pending">Pending</option>
            <option value="processing">Processing</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
          </select>
        </div>
        <div>
          <label htmlFor="source">Source</label>
          <select id="source" value={params.get("source") ?? ""} onChange={(e) => setFilter("source", e.target.value)}>
            <option value="">All</option>
            <option value="api">API</option>
            <option value="s3">Storage</option>
          </select>
        </div>
        <div>
          <label htmlFor="sentiment">Sentiment</label>
          <select
            id="sentiment"
            value={params.get("sentiment") ?? ""}
            onChange={(e) => setFilter("sentiment", e.target.value)}
          >
            <option value="">All</option>
            <option value="positive">Positive</option>
            <option value="neutral">Neutral</option>
            <option value="negative">Negative</option>
          </select>
        </div>
        <div>
          <label htmlFor="min_risk">Minimum risk</label>
          <select
            id="min_risk"
            value={params.get("min_risk") ?? ""}
            onChange={(e) => setFilter("min_risk", e.target.value)}
          >
            <option value="">Any</option>
            <option value="26">26+ moderate</option>
            <option value="51">51+ high</option>
            <option value="76">76+ urgent</option>
          </select>
        </div>
        {hasFilters ? (
          <button type="button" className="secondary" onClick={() => setParams(new URLSearchParams())}>
            Clear filters
          </button>
        ) : null}
      </div>

      <div className="card">
        {isLoading ? (
          <Loading what="Loading conversations" />
        ) : isError ? (
          <ErrorNotice error={error} />
        ) : !data || data.items.length === 0 ? (
          <Empty>
            {hasFilters ? (
              "No conversations match these filters."
            ) : (
              <>
                Nothing here yet. <Link to="/submit">Submit one</Link> or run{" "}
                <code className="mono">make demo-data</code>.
              </>
            )}
          </Empty>
        ) : (
          <>
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Source</th>
                  <th>Status</th>
                  <th>Sentiment</th>
                  <th>Risk</th>
                  <th>Messages</th>
                  <th>Created</th>
                  <th>Completed</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
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
                    <td className="dim mono">{item.message_count}</td>
                    <td className="dim">{formatRelative(item.created_at)}</td>
                    <td className="dim">{formatRelative(item.completed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="spread" style={{ marginTop: 14 }}>
              <span className="faint">
                {offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} of {data.total}
              </span>
              <div className="row">
                <button
                  type="button"
                  className="secondary"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  Previous
                </button>
                <button
                  type="button"
                  className="secondary"
                  disabled={offset + PAGE_SIZE >= data.total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Next
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </>
  );
}
