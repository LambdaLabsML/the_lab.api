/**
 * useLiveRefresh — event-driven data refresh with a slow reconcile fallback.
 *
 * Replaces the per-component `setInterval(refresh, 3–5s)` pattern: the
 * refresh now fires when a matching stream event arrives (debounced, so an
 * event burst costs one fetch), and the interval survives only as a slow
 * reconcile — `fallbackMs` while the event stream is healthy, `fallbackMs/6`
 * (min 3s) while it's down. Hidden tabs skip fallback ticks entirely.
 *
 *   useLiveRefresh(["queue_changed", "experiment_started"], load, 30_000);
 *
 * Pass `types: null` for fallback-only behaviour (no matching event exists).
 */
import { useEffect } from "preact/hooks";
import { subscribeWsEvents, wsConnected } from "../../state/ws";

const DEBOUNCE_MS = 300;

export function useLiveRefresh(
  types: string[] | null,
  refresh: () => unknown,
  fallbackMs = 30_000,
): void {
  useEffect(() => {
    let debounce: ReturnType<typeof setTimeout> | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;

    const fire = () => { try { void refresh(); } catch { /* guard */ } };

    const onEvent = types && types.length
      ? subscribeWsEvents((ev) => {
          if (!types.includes(ev.type)) return;
          if (debounce !== null) return; // burst → one fetch
          debounce = setTimeout(() => {
            debounce = null;
            if (!stopped && !document.hidden) fire();
          }, DEBOUNCE_MS);
        })
      : null;

    const schedule = () => {
      const delay = wsConnected.value
        ? fallbackMs
        : Math.max(3_000, Math.floor(fallbackMs / 6));
      timer = setTimeout(() => {
        if (stopped) return;
        if (!document.hidden) fire();
        schedule();
      }, delay);
    };
    schedule();

    const onVisible = () => { if (!document.hidden) fire(); };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      stopped = true;
      if (debounce !== null) clearTimeout(debounce);
      if (timer !== null) clearTimeout(timer);
      onEvent?.();
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
