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
