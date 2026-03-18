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
  SuggestIdeaRequest,
} from "../lib/types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
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

/** GET /api/v1/experiments/:expId/log */
export async function getExperimentLog(
  expId: number,
  tail?: number,
): Promise<{ log: string }> {
  const qs = tail ? `?tail=${tail}` : "";
  return fetchJson<{ log: string }>(`/api/v1/experiments/${expId}/log${qs}`);
}

/** GET /api/v1/ideas/:ideaId/diff */
export async function getIdeaDiff(
  ideaId: number,
  base?: string,
): Promise<{ stat: string; diff: string; base: string; branch: string; merge_base: string }> {
  const qs = base ? `?base=${encodeURIComponent(base)}` : "";
  return fetchJson(`/api/v1/ideas/${ideaId}/diff${qs}`);
}

/** GET /api/v1/experiments/:expId/progress */
export async function getExperimentProgress(
  expId: number,
): Promise<ProgressResponse> {
  return fetchJson<ProgressResponse>(`/api/v1/experiments/${expId}/progress`);
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
