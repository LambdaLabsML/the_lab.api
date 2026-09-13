import { computed, signal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";

export const chartMetricKeys = signal<string[]>([]);
export const chartHistoryLimit = signal(250);
export const chartHistory = signal({ loaded: 0, total: 0, has_more: false });
const consumers = signal(new Map<symbol, string[]>());
export const chartRequestedMetrics = computed(() => {
  const keys = new Set([...consumers.value.values()].flat().filter(Boolean));
  if (!keys.size) keys.add("score");
  // Always retain live progress without separate per-running-experiment fetches.
  keys.add("pct_complete");
  keys.add("pct");
  return [...keys].sort().join(",");
});

/** Every open chart/table contributes the metric columns it actually uses. */
export function useRequestedMetrics(keys: string[]): void {
  const id = useRef(Symbol());
  const signature = [...new Set(keys.filter(Boolean))].sort().join(",");
  useEffect(() => {
    consumers.value = new Map(consumers.peek()).set(id.current, signature.split(","));
    return () => {
      const next = new Map(consumers.peek());
      next.delete(id.current);
      consumers.value = next;
    };
  }, [signature]);
}
