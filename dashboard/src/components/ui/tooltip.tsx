/**
 * Tooltip — a compact, on-brand hover card built on a robust floating layer.
 *
 * Positioning is two-pass: on hover we capture the trigger rect, render the card
 * hidden, then (in a layout effect, before paint) measure it and clamp it inside
 * the viewport — flipping top↔bottom when there isn't room and nudging left/right
 * so it never spills off-screen. This is the shared base for every hover card
 * (rail status, experiment/idea boxes, summary plots …). See dashboard/DESIGN.md.
 *
 *   <Tooltip content={<>…</>}>{trigger}</Tooltip>
 */
import { useState, useRef, useLayoutEffect, useCallback } from "preact/hooks";
import type { ComponentChildren } from "preact";

const MARGIN = 8; // min gap from viewport edge
const GAP = 8;    // gap between trigger and card

export function Tooltip({
  content,
  children,
  placement = "top",
  class: cls = "",
}: {
  content: ComponentChildren;
  children: ComponentChildren;
  placement?: "top" | "bottom";
  class?: string;
}) {
  const triggerRef = useRef<HTMLSpanElement>(null);
  const cardRef = useRef<HTMLSpanElement>(null);
  const [anchor, setAnchor] = useState<{ left: number; top: number; right: number; bottom: number; cx: number } | null>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  const show = useCallback(() => {
    const el = triggerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setAnchor({ left: r.left, top: r.top, right: r.right, bottom: r.bottom, cx: r.left + r.width / 2 });
    setPos(null);
  }, []);
  const hide = useCallback(() => { setAnchor(null); setPos(null); }, []);

  // Measure the rendered card and clamp it into the viewport (pre-paint).
  useLayoutEffect(() => {
    if (!anchor || !cardRef.current) return;
    const card = cardRef.current.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    let top = placement === "top" ? anchor.top - card.height - GAP : anchor.bottom + GAP;
    // flip if it would clip
    if (placement === "top" && top < MARGIN) top = anchor.bottom + GAP;
    if (placement === "bottom" && top + card.height > vh - MARGIN) top = anchor.top - card.height - GAP;
    top = Math.max(MARGIN, Math.min(top, vh - card.height - MARGIN));

    let left = anchor.cx - card.width / 2;
    left = Math.max(MARGIN, Math.min(left, vw - card.width - MARGIN));

    setPos({ left, top });
  }, [anchor, placement]);

  if (content == null || content === false) return <>{children}</>;

  return (
    <span
      ref={triggerRef}
      class={`ui-tip-trigger ${cls}`}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocusIn={show as any}
      onFocusOut={hide}
    >
      {children}
      {anchor && (
        <span
          ref={cardRef}
          class="ui-tip"
          role="tooltip"
          style={{
            left: `${pos ? pos.left : anchor.left}px`,
            top: `${pos ? pos.top : anchor.top}px`,
            visibility: pos ? "visible" : "hidden",
          }}
        >
          {content}
        </span>
      )}
    </span>
  );
}
