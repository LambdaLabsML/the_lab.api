/**
 * FloatCard — the floating preview shell (see DESIGN.md § Components).
 * Renders fixed, to the RIGHT of its anchor rect and vertically centered on
 * it, clamped to the viewport (two-pass: measure hidden, then place — same
 * approach as <Tooltip>). Without an anchor it falls back to a top-third spot.
 * Non-interactive by design (pointer-events: none) — the hover lives on the
 * trigger. Content is whatever the caller composes (message card, call card…).
 */
import type { ComponentChildren } from "preact";
import { useLayoutEffect, useRef, useState } from "preact/hooks";

export interface FloatAnchor {
  top: number;
  bottom: number;
  right: number;
}

const GAP = 12;   // distance from the anchor's right edge
const MARGIN = 8; // minimum distance to any viewport edge

export function FloatCard({ anchor, children }: {
  anchor?: FloatAnchor | null;
  children: ComponentChildren;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left: number;
    let top: number;
    if (anchor) {
      left = anchor.right + GAP;
      top = (anchor.top + anchor.bottom) / 2 - r.height / 2;
      // never lurk off the right edge — pull back in (may overlap the anchor)
      if (left + r.width > vw - MARGIN) left = Math.max(MARGIN, vw - r.width - MARGIN);
    } else {
      left = Math.max(MARGIN, vw * 0.32);
      top = vh * 0.15;
    }
    top = Math.min(Math.max(MARGIN, top), Math.max(MARGIN, vh - r.height - MARGIN));
    setPos({ left, top });
  }, [children, anchor]);

  return (
    <div
      ref={ref}
      class={`ui-floatcard${pos ? " is-in" : ""}`}
      style={pos ? { left: `${pos.left}px`, top: `${pos.top}px` } : { visibility: "hidden", left: 0, top: 0 }}
      role="presentation"
    >
      {children}
    </div>
  );
}
