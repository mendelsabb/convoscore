// The only way this app reaches anything. Relative URLs throughout: in the container nginx
// proxies /api to the API service, and in `npm run dev` Vite does the same, so there is no
// environment-specific base URL and no CORS to configure.

import type {
  ConversationDetail,
  ConversationFilters,
  ConversationList,
  DemoState,
  FailureMode,
  HealthDetails,
  JobCreated,
  JobStatusResponse,
  Message,
  Stats,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError("Could not reach the API. Is the system running?", 0);
  }

  if (!response.ok) {
    // FastAPI validation errors arrive as a structured `detail`; surface something a human can
    // act on rather than "422".
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") {
        detail = body.detail;
      } else if (Array.isArray(body?.detail) && body.detail.length > 0) {
        detail = body.detail.map((item: { msg?: string }) => item.msg ?? "invalid").join("; ");
      }
    } catch {
      // Keep the status-line fallback.
    }
    throw new ApiError(detail, response.status);
  }

  return (await response.json()) as T;
}

function queryString(filters: ConversationFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "" && value !== null) {
      params.set(key, String(value));
    }
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

export const api = {
  submitConversation: (messages: Message[]) =>
    request<JobCreated>("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ messages }),
    }),

  getJob: (jobId: string) => request<JobStatusResponse>(`/api/jobs/${jobId}`),

  listConversations: (filters: ConversationFilters = {}) =>
    request<ConversationList>(`/api/conversations${queryString(filters)}`),

  getConversation: (id: string) => request<ConversationDetail>(`/api/conversations/${id}`),

  getStats: () => request<Stats>("/api/stats"),

  getHealth: () => request<HealthDetails>("/api/health/details"),

  // Demo controls. These 404 unless the API was started with DEMO_MODE enabled, which is what the
  // Demo page uses to decide whether to render at all.
  getDemoState: () => request<DemoState>("/api/demo/state"),

  armFailure: (mode: FailureMode, count: number) =>
    request<DemoState>("/api/demo/llm-failure", {
      method: "POST",
      body: JSON.stringify({ mode, count }),
    }),

  clearFailures: () => request<DemoState>("/api/demo/llm-failure", { method: "DELETE" }),
};
