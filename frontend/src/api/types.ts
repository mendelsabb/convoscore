// Mirrors the FastAPI response models. The browser only ever talks to /api; it has no access to
// PostgreSQL, SQS, S3 or OpenAI.

export type JobStatus = "pending" | "processing" | "completed" | "failed";
export type Sentiment = "positive" | "neutral" | "negative";
export type Source = "api" | "s3";

export interface ScoreResult {
  sentiment: Sentiment;
  risk_score: number;
  risk_band: string;
  risk_band_label: string;
  rationale: string;
}

export interface JobError {
  type: string;
  message: string | null;
}

export interface JobCreated {
  job_id: string;
  status: JobStatus;
}

export interface JobStatusResponse {
  job_id: string;
  status: JobStatus;
  source: Source;
  attempt_count: number;
  max_attempts: number;
  created_at: string;
  completed_at: string | null;
  result: ScoreResult | null;
  error: JobError | null;
}

export interface ConversationSummary {
  id: string;
  source: Source;
  source_ref: string | null;
  status: JobStatus;
  sentiment: Sentiment | null;
  risk_score: number | null;
  risk_band: string | null;
  message_count: number;
  attempt_count: number;
  created_at: string;
  completed_at: string | null;
}

export interface ConversationList {
  items: ConversationSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface LlmExecution {
  model: string | null;
  prompt_version: string | null;
  schema_version: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  llm_latency_ms: number | null;
  estimated_cost_usd: string | null;
  pricing_version: string | null;
}

export interface Message {
  role: "customer" | "agent";
  content: string;
}

export interface ConversationDetail {
  id: string;
  source: Source;
  source_ref: string | null;
  status: JobStatus;
  conversation: { messages: Message[]; metadata: Record<string, unknown> };
  message_count: number;
  result: ScoreResult | null;
  llm: LlmExecution;
  attempt_count: number;
  max_attempts: number;
  error: JobError | null;
  created_at: string;
  enqueued_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface StatusCounts {
  pending: number;
  processing: number;
  completed: number;
  failed: number;
}

export interface Stats {
  total_jobs: number;
  by_status: StatusCounts;
  by_source: Record<string, number>;
  by_sentiment: Record<string, number>;
  average_risk_score: number | null;
  max_risk_score: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost_usd: string;
  average_llm_latency_ms: number | null;
}

export interface DependencyHealth {
  name: string;
  healthy: boolean;
  detail: string | null;
}

export interface HealthDetails {
  healthy: boolean;
  dependencies: DependencyHealth[];
  config: Record<string, unknown>;
}

export interface ConversationFilters {
  status?: JobStatus;
  source?: Source;
  sentiment?: Sentiment;
  min_risk?: number;
  limit?: number;
  offset?: number;
}
