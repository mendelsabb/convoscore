import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { FailureMode } from "../api/types";
import { Card, ErrorNotice, Loading } from "../components/Common";

const MODES: { mode: FailureMode; title: string; what: string }[] = [
  {
    mode: "timeout",
    title: "Provider timeout",
    what: "The scoring call waits, then fails the way a real client timeout does. Classified as transient, so it is retried.",
  },
  {
    mode: "http_500",
    title: "Provider 500",
    what: "The provider returns a server error. Also transient: retried with exponential backoff via the queue.",
  },
  {
    mode: "malformed",
    title: "Malformed response",
    what: "The model answers outside the contract. Rejected by the same validator that guards real responses, and retried once before failing.",
  },
];

export function Demo() {
  const [count, setCount] = useState(1);
  const queryClient = useQueryClient();

  const state = useQuery({
    queryKey: ["demo-state"],
    queryFn: api.getDemoState,
    refetchInterval: 2000,
  });

  const arm = useMutation({
    mutationFn: ({ mode, n }: { mode: FailureMode; n: number }) => api.armFailure(mode, n),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["demo-state"] }),
  });

  const clear = useMutation({
    mutationFn: api.clearFailures,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["demo-state"] }),
  });

  if (state.isLoading) return <Loading what="Loading demo controls" />;

  if (state.isError) {
    return (
      <>
        <h1>Demo controls</h1>
        <div className="notice notice-info">
          Demo mode is off, so these controls are not available. The endpoints are not registered
          at all unless <code className="mono">DEMO_MODE=true</code>.
        </div>
      </>
    );
  }

  const armed = state.data?.armed ?? {};
  const totalArmed = state.data?.total_armed ?? 0;

  return (
    <>
      <h1>Demo controls</h1>
      <p className="page-intro">
        These arm deliberate failures in the scoring path. Nothing here writes to Prometheus or
        edits a dashboard: the next scoring call genuinely fails, and everything downstream is the
        ordinary code path.
      </p>

      {totalArmed > 0 ? (
        <div className="notice notice-error">
          <strong>{totalArmed} deliberate failure{totalArmed === 1 ? "" : "s"} armed.</strong> The
          next scoring calls will fail on purpose. Clear them when you are done, or the next
          person to use this system will think it is broken.
        </div>
      ) : null}

      {arm.isError ? <ErrorNotice error={arm.error} /> : null}

      <Card title="Arm a failure">
        <div className="row" style={{ marginBottom: 16 }}>
          <label htmlFor="count" style={{ margin: 0 }}>
            How many calls should fail
          </label>
          <select
            id="count"
            value={count}
            onChange={(event) => setCount(Number(event.target.value))}
            style={{ width: 90 }}
          >
            <option value={1}>1</option>
            <option value={2}>2</option>
            <option value={4}>4</option>
            <option value={8}>8</option>
          </select>
          <span className="faint">
            One failure is absorbed by a retry. Four exhausts the three attempts and fails the job.
          </span>
        </div>

        <div className="two-col">
          {MODES.map(({ mode, title, what }) => (
            <div key={mode} className="card" style={{ marginTop: 0 }}>
              <div className="spread" style={{ marginBottom: 8 }}>
                <strong>{title}</strong>
                {armed[mode] ? <span className="badge badge-failed">{armed[mode]} armed</span> : null}
              </div>
              <p className="faint" style={{ marginTop: 0 }}>
                {what}
              </p>
              <button
                type="button"
                onClick={() => arm.mutate({ mode, n: count })}
                disabled={arm.isPending}
              >
                Arm {count}
              </button>
            </div>
          ))}
        </div>

        <div className="row" style={{ marginTop: 16 }}>
          <button
            type="button"
            className="secondary"
            onClick={() => clear.mutate()}
            disabled={clear.isPending || totalArmed === 0}
          >
            Disarm everything
          </button>
          <Link to="/submit">Submit a conversation →</Link>
        </div>
      </Card>

      <Card title="What to watch">
        <ol style={{ margin: 0, paddingLeft: 20, lineHeight: 1.9 }}>
          <li>
            Arm a failure, then <Link to="/submit">submit a conversation</Link>.
          </li>
          <li>
            The job goes <span className="mono">pending → processing</span>, fails, and returns to{" "}
            <span className="mono">pending</span> for a retry. The attempt counter rises on the
            conversation detail page.
          </li>
          <li>
            In Grafana, the <em>Scoring provider</em> row shows the failure by outcome and the
            retry counter moving. Prometheus discovers this by scraping as usual.
          </li>
          <li>
            With four or more armed, the job exhausts its attempts and lands in{" "}
            <span className="mono">failed</span> with the reason recorded.
          </li>
        </ol>
      </Card>

      <Card title="Infrastructure failure">
        <p style={{ marginTop: 0 }}>
          Killing pods is deliberately <strong>not</strong> possible from this page. The
          application has no Kubernetes permissions and no service account token mounted, so it
          cannot delete anything. That is a property worth keeping, not a gap.
        </p>
        <p className="faint" style={{ marginBottom: 0 }}>
          Run these from a terminal instead:
        </p>
        <pre
          className="mono"
          style={{
            background: "var(--bg)",
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: 12,
            overflowX: "auto",
          }}
        >
{`make demo-infra-failure              # kill a worker, watch Kubernetes replace it
make demo-infra-failure TARGET=api   # kill the API instead
make demo-restart                    # restart everything, prove results survived`}
        </pre>
      </Card>
    </>
  );
}
