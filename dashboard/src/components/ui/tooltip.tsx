/**
 * Tooltip — a compact, on-brand hover card. Wrap any (inline) trigger:
 *   <Tooltip content={<>…</>}>{child}</Tooltip>
 * The card is position:fixed (so it escapes panel overflow clipping), centered
 * above the trigger, and flips below when near the top edge. content==null →
 * renders the child with no tooltip. See dashboard/DESIGN.md.
 */
import { useState, useRef, useCallback } from "preact/hooks";
import type { ComponentChildren } from "preact";

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
  const [box, setBox] = useState<{ x: number; y: number; place: "top" | "bottom" } | null>(null);
  const ref = useRef<HTMLSpanElement>(null);

  const show = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    // flip below when too close to the top of the viewport
    const place: "top" | "bottom" = placement === "top" && r.top < 96 ? "bottom" : placement;
    setBox({
      x: Math.round(r.left + r.width / 2),
      y: Math.round(place === "top" ? r.top : r.bottom),
      place,
    });
  }, [placement]);

  const hide = useCallback(() => setBox(null), []);

  if (content == null || content === false) return <>{children}</>;

  return (
    <span
      ref={ref}
      class={`ui-tip-trigger ${cls}`}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocusIn={show as any}
      onFocusOut={hide}
    >
      {children}
      {box && (
        <span
          class={`ui-tip ui-tip--${box.place}`}
          role="tooltip"
          style={{ left: `${box.x}px`, top: `${box.y}px` }}
        >
          {content}
        </span>
      )}
    </span>
  );
}
