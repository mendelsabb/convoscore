import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { JobStatus } from "../api/types";
import { RiskBadge, SentimentLabel, StatusBadge } from "../components/Badges";
import { Card, ErrorNotice } from "../components/Common";
import { EXAMPLE_TRANSCRIPT, parseTranscript } from "../lib/transcript";

const LIFECYCLE: JobStatus[] = ["pending", "processing", "completed"];

export function Submit() {
  const [text, setText] = useState(EXAMPLE_TRANSCRIPT);
  const [jobId, setJobId] = useState<string | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const submit = useMutation({
    mutationFn: (raw: string) => {
      const { messages, error } = parseTranscript(raw);
      if (error) return Promise.reject(new Error(error));
      return api.submitConversation(messages);
    },
    onSuccess: (created) => {
      setJobId(created.job_id);
      queryClient.invalidateQueries({ queryKey: ["stats"] });
    },
  });

  // Poll the job until it reaches a terminal state. This is the asynchronous pipeline made
  // visible: the request returned immediately with an id, and the result arrives later.
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.getJob(jobId as string),
    enabled: jobId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "completed" || status === "failed" ? false : 1000;
    },
  });

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    const { error } = parseTranscript(text);
    setParseError(error);
    if (error) return;
    setJobId(null);
    submit.mutate(text);
  }

  return (
    <>
      <h1>Submit a conversation</h1>
      <p className="page-intro">
        The API validates and stores the conversation, queues it, and returns a job id straight
        away. A worker scores it a moment later.
      </p>

      <div className="two-col">
        <Card title="Conversation">
          <form onSubmit={onSubmit}>
            <label htmlFor="transcript">
              One message per line, prefixed with the speaker.
            </label>
            <textarea
              id="transcript"
              value={text}
              onChange={(event) => {
                setText(event.target.value);
                setParseError(null);
              }}
              spellCheck={false}
            />
            {parseError ? <div className="notice notice-error" style={{ marginTop: 12 }}>{parseError}</div> : null}
            {submit.isError ? (
              <div style={{ marginTop: 12 }}>
                <ErrorNotice error={submit.error} />
              </div>
            ) : null}
            <div className="row" style={{ marginTop: 14 }}>
              <button type="submit" disabled={submit.isPending}>
                {submit.isPending ? "Submitting…" : "Score conversation"}
              </button>
              <button
                type="button"
                className="secondary"
                onClick={() => {
                  setText(EXAMPLE_TRANSCRIPT);
                  setParseError(null);
                }}
              >
                Reset example
              </button>
            </div>
          </form>
        </Card>

        <Card title="Result">
          {!jobId ? (
            <p className="faint">Submit a conversation to watch it move through the pipeline.</p>
          ) : (
            <>
              <dl className="kv">
                <dt>Job ID</dt>
                <dd>{jobId}</dd>
              </dl>

              <Lifecycle status={job.data?.status ?? "pending"} />

              {job.data?.status === "completed" && job.data.result ? (
                <>
                  <dl className="kv" style={{ marginTop: 16 }}>
                    <dt>Sentiment</dt>
                    <dd>
                      <SentimentLabel sentiment={job.data.result.sentiment} />
                    </dd>
                    <dt>Risk</dt>
                    <dd>
                      <RiskBadge
                        score={job.data.result.risk_score}
                        band={job.data.result.risk_band}
                      />{" "}
                      <span className="muted">{job.data.result.risk_band_label}</span>
                    </dd>
                    <dt>Attempts</dt>
                    <dd>{job.data.attempt_count}</dd>
                  </dl>
                  <p className="rationale" style={{ marginTop: 14 }}>
                    {job.data.result.rationale}
                  </p>
                  <p style={{ marginTop: 16, marginBottom: 0 }}>
                    <Link to={`/conversations/${jobId}`}>Open full detail →</Link>
                  </p>
                </>
              ) : null}

              {job.data?.status === "failed" ? (
                <div className="notice notice-error" style={{ marginTop: 16 }}>
                  <strong>{job.data.error?.type ?? "failed"}</strong>
                  {job.data.error?.message ? <div style={{ marginTop: 6 }}>{job.data.error.message}</div> : null}
                  <div style={{ marginTop: 6 }} className="faint">
                    Gave up after {job.data.attempt_count} of {job.data.max_attempts} attempts.
                  </div>
                </div>
              ) : null}

              {job.data && job.data.status !== "completed" && job.data.status !== "failed" ? (
                <p className="faint" style={{ marginTop: 14, marginBottom: 0 }}>
                  <span className="spin" /> Waiting for a worker to pick this up…
                </p>
              ) : null}
            </>
          )}
        </Card>
      </div>

      <Card title="Recently submitted">
        <RecentlySubmitted />
      </Card>
    </>
  );
}

function Lifecycle({ status }: { status: JobStatus }) {
  if (status === "failed") {
    return (
      <div className="steps">
        <span className="step done">pending</span>
        <span className="step-arrow">→</span>
        <span className="step done">processing</span>
        <span className="step-arrow">→</span>
        <StatusBadge status="failed" />
      </div>
    );
  }

  const currentIndex = LIFECYCLE.indexOf(status);
  return (
    <div className="steps">
      {LIFECYCLE.map((step, index) => (
        <span key={step} style={{ display: "contents" }}>
          {index > 0 ? <span className="step-arrow">→</span> : null}
          <span
            className={`step${index < currentIndex ? " done" : ""}${index === currentIndex ? " current" : ""}`}
          >
            {step}
          </span>
        </span>
      ))}
    </div>
  );
}

function RecentlySubmitted() {
  const { data } = useQuery({
    queryKey: ["conversations", { source: "api", limit: 5 }],
    queryFn: () => api.listConversations({ source: "api", limit: 5 }),
    refetchInterval: 4000,
  });

  if (!data || data.items.length === 0) {
    return <p className="faint" style={{ margin: 0 }}>Nothing submitted through the API yet.</p>;
  }

  return (
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Status</th>
          <th>Sentiment</th>
          <th>Risk</th>
          <th>Tokens</th>
        </tr>
      </thead>
      <tbody>
        {data.items.map((item) => (
          <tr key={item.id}>
            <td className="mono">
              <Link to={`/conversations/${item.id}`}>{item.id.slice(0, 8)}</Link>
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
            <td className="dim mono">{item.message_count} msgs</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
