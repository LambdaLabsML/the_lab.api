// ------------------------------------------------------------
// Typed fetch wrappers for all API endpoints used by the dashboard.
// Every function returns a typed Promise; callers decide what to
// do with the data (typically write it into a signal).
// ------------------------------------------------------------

import type {
  AgentEntry,
  BacklogResponse,
  GraphResponse,
  ChartDataResponse,
  IdeaDetail,
  Experiment,
  MessageEntry,
  ProgressResponse,
  OpenAPISpec,
  PromptMeta,
  QueueSnapshot,
  ResourceState,
  ResourceUpsertBody,
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
  // Always request full experiments so the detail panel shows meta/config keys.
  // The slim default strips meta (it's designed for agent token budgets, not the UI).
  const params = new URLSearchParams({ experiments: "full" });
  if (allNotes) params.set("notes", "all");
  return fetchJson<IdeaDetail>(`/api/v1/ideas/${id}?${params}`);
}

/** GET /api/v1/ideas — returns the full list of ideas (summary form).
 *
 * Pass `fields` to project down to a specific subset of keys, reducing
 * payload size significantly on large labs. The `?fields=` param is
 * handled server-side and also skips building fields that weren't
 * requested (e.g. experiment_summary).
 */
export async function getAllIdeas(fields?: string): Promise<IdeaDetail[]> {
  const qs = fields ? `?fields=${encodeURIComponent(fields)}` : "";
  return fetchJson<IdeaDetail[]>(`/api/v1/ideas${qs}`);
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
  // UI always requests the full log — tail=25 default is for agent API calls.
  const qs = tail ? `?tail=${tail}` : "?full=true";
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
): Promise<{ output: string; base_path: string; format?: "md" | "html" }> {
  return fetchJson<{ output: string; base_path: string; format?: "md" | "html" }>(
    `/api/v1/experiments/${expRef}/output`,
  );
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
  response_sizes: { endpoint: string; calls: number; total_kb: number; avg_kb: number; max_kb: number }[];
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
  file_rw: string[];
  file_ro: string[];
  disable_password?: string;
}): Promise<SandboxState> {
  return fetchJson<SandboxState>("/api/v1/sandbox", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------

/** GET /api/v1/prompts */
export async function listPrompts(): Promise<PromptMeta[]> {
  return fetchJson<PromptMeta[]>("/api/v1/prompts");
}

/** GET /api/v1/prompts/:role */
export async function getPrompt(role: string): Promise<{ role: string; content: string }> {
  return fetchJson<{ role: string; content: string }>(`/api/v1/prompts/${encodeURIComponent(role)}`);
}

/** PUT /api/v1/prompts/:role */
export async function putPrompt(role: string, content: string): Promise<PromptMeta> {
  return fetchJson<PromptMeta>(`/api/v1/prompts/${encodeURIComponent(role)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
}

/** DELETE /api/v1/prompts/:role */
export async function deletePromptRole(role: string): Promise<{ status: string; role: string }> {
  return fetchJson<{ status: string; role: string }>(`/api/v1/prompts/${encodeURIComponent(role)}`, {
    method: "DELETE",
  });
}

// ---------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------

/** GET /api/v1/agents */
export async function listAgents(): Promise<AgentEntry[]> {
  return fetchJson<AgentEntry[]>("/api/v1/agents");
}

/** GET /api/v1/messages */
export async function listMessages(limit: number = 100): Promise<MessageEntry[]> {
  return fetchJson<MessageEntry[]>(`/api/v1/messages?limit=${limit}`);
}

/** DELETE /api/v1/agents/:id?keep_branch=true|false */
export async function unregisterAgent(
  id: string,
  keepBranch: boolean = true,
): Promise<{ status: string; agent_id: string }> {
  const qs = `?keep_branch=${keepBranch ? "true" : "false"}`;
  return fetchJson<{ status: string; agent_id: string }>(
    `/api/v1/agents/${encodeURIComponent(id)}${qs}`,
    { method: "DELETE" },
  );
}

// ---------------------------------------------------------------------------
// Queue + Resources
// ---------------------------------------------------------------------------

/** GET /api/v1/queue */
export async function getQueue(): Promise<QueueSnapshot> {
  return fetchJson<QueueSnapshot>("/api/v1/queue");
}

/** GET /api/v1/queue/config */
export async function getQueueConfig(): Promise<{ paused: boolean; dispatch_interval_s: number }> {
  return fetchJson<{ paused: boolean; dispatch_interval_s: number }>("/api/v1/queue/config");
}

/** PUT /api/v1/queue/config */
export async function setQueueConfig(req: {
  paused?: boolean;
  dispatch_interval_s?: number;
}): Promise<{ paused: boolean; dispatch_interval_s: number }> {
  return fetchJson<{ paused: boolean; dispatch_interval_s: number }>("/api/v1/queue/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

/** POST /api/v1/queue/pause */
export async function pauseQueue(): Promise<{ paused: boolean; dispatch_interval_s: number }> {
  return fetchJson<{ paused: boolean; dispatch_interval_s: number }>("/api/v1/queue/pause", {
    method: "POST",
  });
}

/** POST /api/v1/queue/resume */
export async function resumeQueue(): Promise<{ paused: boolean; dispatch_interval_s: number }> {
  return fetchJson<{ paused: boolean; dispatch_interval_s: number }>("/api/v1/queue/resume", {
    method: "POST",
  });
}

/** GET /api/v1/resources */
export async function listResources(): Promise<ResourceState[]> {
  return fetchJson<ResourceState[]>("/api/v1/resources");
}

/** GET /api/v1/resources/:name */
export async function getResource(name: string): Promise<ResourceState> {
  return fetchJson<ResourceState>(`/api/v1/resources/${encodeURIComponent(name)}`);
}

/** PUT /api/v1/resources/:name */
export async function upsertResource(
  name: string,
  body: ResourceUpsertBody,
): Promise<ResourceState> {
  return fetchJson<ResourceState>(`/api/v1/resources/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** DELETE /api/v1/resources/:name */
export async function deleteResource(name: string): Promise<{ status: string; name: string }> {
  return fetchJson<{ status: string; name: string }>(
    `/api/v1/resources/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
}

/** POST /api/v1/experiments/:ref/priority */
export async function setExperimentPriority(
  ref: string | number,
  priority: number,
): Promise<unknown> {
  return fetchJson<unknown>(`/api/v1/experiments/${ref}/priority`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ priority }),
  });
}

/** POST /api/v1/experiments/:ref/cancel */
export async function cancelExperiment(ref: string | number): Promise<unknown> {
  return fetchJson<unknown>(`/api/v1/experiments/${ref}/cancel`, {
    method: "POST",
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
