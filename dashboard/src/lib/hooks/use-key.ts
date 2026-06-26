/**
 * useKey / useEscape — window-level keyboard handlers with automatic cleanup.
 * Replaces the hand-rolled addEventListener("keydown", …) blocks for Escape-to-close.
 */
import { useEffect } from "preact/hooks";

export function useKey(
  key: string,
  handler: (e: KeyboardEvent) => void,
  enabled = true,
) {
  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === key) handler(e);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [key, handler, enabled]);
}

export function useEscape(handler: (e: KeyboardEvent) => void, enabled = true) {
  useKey("Escape", handler, enabled);
}
