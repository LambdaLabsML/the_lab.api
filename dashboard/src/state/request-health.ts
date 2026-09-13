import { signal } from "@preact/signals";

export const lastApiSuccess = signal<number | null>(null);
export const apiFailures = signal<Record<string, string>>({});

export function recordRequest(url: string, error?: unknown): void {
  const key = url.split("?")[0];
  const next = { ...apiFailures.peek() };
  if (error !== undefined) next[key] = error instanceof Error ? error.message : String(error);
  else {
    delete next[key];
    lastApiSuccess.value = Date.now();
  }
  apiFailures.value = next;
}

/** Clear a stale failure for an expected non-success response without
 * claiming it was an API success (for example, output that is not written yet). */
export function clearRequestFailure(url: string): void {
  const key = url.split("?")[0];
  const next = { ...apiFailures.peek() };
  delete next[key];
  apiFailures.value = next;
}
