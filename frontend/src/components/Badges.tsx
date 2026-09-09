import type { JobStatus, Sentiment, Source } from "../api/types";
import { titleCase } from "../lib/format";

export function StatusBadge({ status }: { status: JobStatus }) {
  return <span className={`badge badge-${status}`}>{titleCase(status)}</span>;
}

export function SourceBadge({ source }: { source: Source }) {
  // The label spells out what the source means; "api" and "s3" alone are jargon on a review screen.
  const label = source === "api" ? "API" : "Storage";
  return <span className="badge badge-source">{label}</span>;
}

export function SentimentLabel({ sentiment }: { sentiment: Sentiment | null }) {
  if (!sentiment) return <span className="faint">—</span>;
  return <span className={`sentiment-${sentiment}`}>{titleCase(sentiment)}</span>;
}

const BAND_CLASS: Record<string, string> = {
  low_concern: "risk-low",
  moderate_concern: "risk-moderate",
  high_concern: "risk-high",
  urgent_escalation: "risk-urgent",
};

/**
 * Risk is shown as a number and a bar. The bar is what makes a list scannable: a reviewer
 * triaging thirty conversations should be able to see where to look without reading every score.
 */
export function RiskBadge({ score, band }: { score: number | null; band: string | null }) {
  if (score === null || score === undefined) return <span className="faint">—</span>;
  const className = BAND_CLASS[band ?? ""] ?? "risk-low";
  return (
    <span className={`risk ${className}`} title={band?.replace(/_/g, " ")}>
      <span className="risk-score">{score}</span>
      <span className="risk-bar">
        <div style={{ width: `${score}%` }} />
      </span>
    </span>
  );
}
