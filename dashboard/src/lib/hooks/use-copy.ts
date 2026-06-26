/**
 * useCopyToClipboard — copy text with transient "copied" feedback.
 * Unifies the copy-launch-command / copy-agent-id implementations that each
 * rolled their own state machine + timeout.
 */
import { useState, useRef, useCallback } from "preact/hooks";

export function useCopyToClipboard(resetMs = 1500) {
  const [state, setState] = useState<"idle" | "copied" | "error">("idle");
  const timer = useRef<number | undefined>(undefined);

  const copy = useCallback(
    async (text: string) => {
      if (timer.current) window.clearTimeout(timer.current);
      try {
        await navigator.clipboard.writeText(text);
        setState("copied");
      } catch {
        setState("error");
      }
      timer.current = window.setTimeout(() => setState("idle"), resetMs);
    },
    [resetMs],
  );

  return { state, copied: state === "copied", error: state === "error", copy };
}
