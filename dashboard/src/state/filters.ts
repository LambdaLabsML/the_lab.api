/**
 * Experiment filters: the active selection (a saved filter and/or an ad-hoc
 * expression) that charts, the table and idea means are computed from.
 * The server evaluates expressions (it has every experiment's settings) and
 * returns the matching labels; see the_lab/filters.py for the language.
 * Saved filters are created and edited by humans only.
 */
import { computed, effect, signal } from "@preact/signals";
import type { Experiment } from "../lib/types";
import { humanIdForName } from "../lib/identity";
import { allExperiments } from "./signals";
import { myName, useSetting } from "./settings";

export interface SavedFilter {
  id: string;
  name: string;
  expr: string;
  exclude: string[];
  description: string;
  owner: string;
  shared: boolean;
  copied_from?: string | null;
  created_at: string;
  updated_at: string;
}

export interface FilterCheck { ok: boolean; error: string | null; matched: number; total: number | null; sample: string[] }

/** The active selection, per browser: a saved filter id and/or an expression. */
export const activeSelection = useSetting<{ filter: string | null; where: string }>(
  "experimentSelection", { filter: null, where: "" });

export const savedFilters = signal<SavedFilter[]>([]);
/** Labels in the active selection; null = no selection (everything). */
export const selectionLabels = signal<Set<string> | null>(null);
export const selectionError = signal<string | null>(null);

// The selection may name a filter by id (picked in the UI) or by name (a
// shared link: ?filter=long); the server accepts both too.
export const activeFilter = computed(() => {
  const ref = (activeSelection.value.filter || "").toLowerCase();
  return savedFilters.value.find((f) => f.id === activeSelection.value.filter || f.name.toLowerCase() === ref) ?? null;
});

export function myHumanId(): string {
  return humanIdForName(myName.value || "") ?? "user";
}

const headers = { "Content-Type": "application/json", "X-The-Lab-Source": "dashboard" };

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { ...init, headers: { ...headers, ...(init?.headers || {}) } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body?.detail === "string" ? body.detail : `${res.status}`);
  return body as T;
}

export async function loadFilters(): Promise<void> {
  try {
    const r = await call<{ filters: SavedFilter[] }>("/api/v1/filters");
    savedFilters.value = r.filters;
  } catch { /* keep the last list */ }
}

export function checkFilter(where: string, filter?: string | null): Promise<FilterCheck> {
  const qs = new URLSearchParams({ sample: "5" });
  if (where.trim()) qs.set("where", where);
  if (filter) qs.set("filter", filter);
  return call<FilterCheck>(`/api/v1/filters/check?${qs}`);
}

const asUser = () => (myName.value || "").trim() || undefined;

export async function createFilter(body: { name: string; expr: string; description?: string; shared?: boolean; exclude?: string[] }): Promise<SavedFilter> {
  const rec = await call<SavedFilter>("/api/v1/filters", { method: "POST", body: JSON.stringify({ ...body, as_user: asUser() }) });
  await loadFilters();
  return rec;
}

export async function updateFilter(id: string, changes: Partial<Pick<SavedFilter, "name" | "expr" | "description" | "shared" | "exclude">>
  & { add_exclude?: string[]; remove_exclude?: string[] }): Promise<SavedFilter> {
  const rec = await call<SavedFilter>(`/api/v1/filters/${encodeURIComponent(id)}`, {
    method: "PATCH", body: JSON.stringify({ ...changes, as_user: asUser() }),
  });
  await loadFilters();
  void refreshSelection();
  return rec;
}

export async function copyFilter(id: string, name: string, shared = false): Promise<SavedFilter> {
  const rec = await call<SavedFilter>(`/api/v1/filters/${encodeURIComponent(id)}/copy`, {
    method: "POST", body: JSON.stringify({ name, shared, as_user: asUser() }),
  });
  await loadFilters();
  return rec;
}

export async function archiveFilter(id: string): Promise<void> {
  const qs = asUser() ? `?as_user=${encodeURIComponent(asUser()!)}` : "";
  await call(`/api/v1/filters/${encodeURIComponent(id)}${qs}`, { method: "DELETE" });
  if (activeSelection.value.filter === id) activeSelection.value = { ...activeSelection.value, filter: null };
  await loadFilters();
}

let seq = 0;
export async function refreshSelection(): Promise<void> {
  const { filter, where } = activeSelection.value;
  const mine = ++seq;
  if (!filter && !where.trim()) {
    selectionLabels.value = null;
    selectionError.value = null;
    return;
  }
  const qs = new URLSearchParams();
  if (filter) qs.set("filter", filter);
  if (where.trim()) qs.set("where", where);
  try {
    const r = await call<{ labels: string[] }>(`/api/v1/filters/match?${qs}`);
    if (mine !== seq) return;
    selectionLabels.value = new Set(r.labels);
    selectionError.value = null;
  } catch (e) {
    if (mine !== seq) return;
    selectionError.value = String((e as Error).message || e);
    // A filter that no longer exists / no longer parses selects nothing
    // rather than silently everything.
    selectionLabels.value = new Set();
  }
}

/** Whether *e* is in the active selection (always true without one). */
export function inSelection(e: Pick<Experiment, "label" | "id">): boolean {
  const s = selectionLabels.value;
  return !s || s.has(String(e.label ?? e.id));
}

/** The experiments charts, table and idea means are computed from. */
export const plotExperiments = computed(() => {
  const s = selectionLabels.value;
  return s ? allExperiments.value.filter((e) => s.has(String(e.label ?? e.id))) : allExperiments.value;
});

// Re-evaluate when the selection changes, and (debounced) when experiments
// arrive or change — a new run may match the filter.
let timer: number | null = null;
let lastCount = -1;
effect(() => {
  void activeSelection.value;
  void refreshSelection();
});
effect(() => {
  const n = allExperiments.value.length;
  if (n === lastCount) return;
  lastCount = n;
  if (timer !== null) window.clearTimeout(timer);
  timer = window.setTimeout(() => { timer = null; void refreshSelection(); }, 1500);
});
void loadFilters();
