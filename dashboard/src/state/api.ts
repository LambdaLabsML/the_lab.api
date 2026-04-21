// ------------------------------------------------------------
// Typed fetch wrappers for all API endpoints used by the dashboard.
// Every function returns a typed Promise; callers decide what to
// do with the data (typically write it into a signal).
// ------------------------------------------------------------

import type {
  BacklogResponse,
  GraphResponse,
  ChartDataResponse,
  IdeaDetail,
  Experiment,
  ProgressResponse,
  OpenAPISpec,
  SandboxState,
  SuggestIdeaRequest,
} from "../lib/types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("X-The-Lab-Source", "dashboard");
  const res = await fetch(url, { ...init, headers });
  if (!res.ok) {
    throw new Error(`${init?.method ?? "GET"} ${url} returned ${res.status}`);
  }
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Orientation
// ---------------------------------------------------------------------------

/** GET /api/v1/backlog */
export async function getBacklog(): Promise<BacklogResponse> {
  return fetchJson<BacklogResponse>("/api/v1/backlog");
}

/** GET /api/v1/graph */
export async function getGraph(): Promise<GraphResponse> {
  return fetchJson<GraphResponse>("/api/v1/graph");
}

/** GET /api/v1/chart-data */
export async function getChartData(): Promise<ChartDataResponse> {
  return fetchJson<ChartDataResponse>("/api/v1/chart-data");
}

// ---------------------------------------------------------------------------
// Ideas
// ---------------------------------------------------------------------------

/**
 * GET /api/v1/ideas/:id
 * When `allNotes` is true the query param `?notes=all` is appended so the
 * response includes every note attached to the idea.
 */
export async function getIdea(
  id: number,
  allNotes?: boolean,
): Promise<IdeaDetail> {
  const qs = allNotes ? "?notes=all" : "";
  return fetchJson<IdeaDetail>(`/api/v1/ideas/${id}${qs}`);
}

/** GET /api/v1/ideas — returns the full list of ideas (summary form). */
export async function getAllIdeas(): Promise<IdeaDetail[]> {
  return fetchJson<IdeaDetail[]>("/api/v1/ideas");
}

/** POST /api/v1/ideas/suggest */
export async function suggestIdea(req: SuggestIdeaRequest): Promise<unknown> {
  return fetchJson<unknown>("/api/v1/ideas/suggest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

// ---------------------------------------------------------------------------
// Experiments
// ---------------------------------------------------------------------------

/** GET /api/v1/ideas/:ideaId/experiments (if the endpoint exists) */
export async function getIdeaExperiments(
  ideaId: number,
): Promise<Experiment[]> {
  return fetchJson<Experiment[]>(`/api/v1/ideas/${ideaId}/experiments`);
}

/** GET /api/v1/experiments/:expRef/log */
export async function getExperimentLog(
  expRef: string | number,
  tail?: number,
): Promise<{ log: string }> {
  const qs = tail ? `?tail=${tail}` : "";
  return fetchJson<{ log: string }>(`/api/v1/experiments/${expRef}/log${qs}`);
}

/** GET /api/v1/ideas/:ideaId/diff */
export async function getIdeaDiff(
  ideaId: number,
  base?: string,
): Promise<{ stat: string; diff: string; base: string; branch: string; merge_base: string }> {
  const qs = base ? `?base=${encodeURIComponent(base)}` : "";
  return fetchJson(`/api/v1/ideas/${ideaId}/diff${qs}`);
}

/** GET /api/v1/experiments/:expRef/script */
export async function getExperimentScript(
  expRef: string | number,
): Promise<{ script: string }> {
  return fetchJson<{ script: string }>(`/api/v1/experiments/${expRef}/script`);
}

/** GET /api/v1/experiments/:expRef/output */
export async function getExperimentOutput(
  expRef: string | number,
): Promise<{ output: string; base_path: string }> {
  return fetchJson<{ output: string; base_path: string }>(`/api/v1/experiments/${expRef}/output`);
}

/** GET /api/v1/experiments/:expRef/progress */
export async function getExperimentProgress(
  expRef: string | number,
): Promise<ProgressResponse> {
  return fetchJson<ProgressResponse>(`/api/v1/experiments/${expRef}/progress`);
}

// ---------------------------------------------------------------------------
// Tags
// ---------------------------------------------------------------------------

/** POST /api/v1/experiments/tags/rename */
export async function renameTag(
  oldTag: string,
  newTag: string,
): Promise<unknown> {
  return fetchJson<unknown>("/api/v1/experiments/tags/rename", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ old: oldTag, new: newTag }),
  });
}

// ---------------------------------------------------------------------------
// OpenAPI spec (used by the API explorer view)
// ---------------------------------------------------------------------------

/** GET /openapi.json */
export async function getOpenApiSpec(): Promise<OpenAPISpec> {
  return fetchJson<OpenAPISpec>("/openapi.json");
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------

export interface ApiStatsResponse {
  total_calls: number;
  pattern_length: number;
  calls: { endpoint: string; count: number }[];
  patterns: { sequence: string; count: number }[];
}

/** GET /api/v1/stats */
export async function getApiStats(patternLength = 2): Promise<ApiStatsResponse> {
  return fetchJson<ApiStatsResponse>(`/api/v1/stats?pattern_length=${patternLength}`);
}

// ---------------------------------------------------------------------------
// Sandbox
// ---------------------------------------------------------------------------

/** GET /api/v1/sandbox */
export async function getSandboxState(): Promise<SandboxState> {
  return fetchJson<SandboxState>("/api/v1/sandbox");
}

/** PUT /api/v1/sandbox */
export async function updateSandboxState(req: {
  enabled: boolean;
  allowlist: string[];
  denylist: string[];
}): Promise<SandboxState> {
  return fetchJson<SandboxState>("/api/v1/sandbox", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

/** GET /api/v1/chat/status */
export async function getChatStatus(): Promise<{ available: boolean }> {
  return fetchJson<{ available: boolean }>("/api/v1/chat/status");
}

/**
 * POST /api/v1/chat — streams Claude's response via SSE.
 * Calls `onText` for each text chunk and `onDone` when finished.
 */
export async function streamChat(
  messages: Array<{ role: string; content: string }>,
  onText: (text: string) => void,
  onDone: () => void,
  onError: (err: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/v1/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
    signal,
  });
  if (!res.ok) {
    const body = await res.text();
    onError(`HTTP ${res.status}: ${body}`);
    return;
  }
  const reader = res.body?.getReader();
  if (!reader) {
    onError("No response body");
    return;
  }
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      try {
        const evt = JSON.parse(line.slice(6));
        if (evt.type === "text") onText(evt.text);
        else if (evt.type === "done") onDone();
        else if (evt.type === "error") onError(evt.error);
      } catch { /* skip malformed lines */ }
    }
  }
  onDone();
}
