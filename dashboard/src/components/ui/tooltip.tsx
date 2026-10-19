/** The same positioned, dismissible hover card for DOM, SVG and canvas. */
import { createPortal } from "preact/compat";
import type { ComponentChildren } from "preact";
import { useLayoutEffect, useRef, useState } from "preact/hooks";
import { useHoverPreview } from "../../lib/hooks/use-hover-preview";

export function HoverCard({ anchor, children, placement = "top" }: {
  anchor: DOMRect; children: ComponentChildren; placement?: "top" | "bottom";
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const place = () => {
      const card = element.getBoundingClientRect();
      let top = placement === "top" ? anchor.top - card.height - 8 : anchor.bottom + 8;
      if (top < 8) top = anchor.bottom + 8;
      if (top + card.height > innerHeight - 8) top = anchor.top - card.height - 8;
      top = Math.max(8, Math.min(top, innerHeight - card.height - 8));
      const left = Math.max(8, Math.min(anchor.left + anchor.width / 2 - card.width / 2, innerWidth - card.width - 8));
      setPosition(previous => previous?.left === left && previous.top === top ? previous : { left, top });
    };
    place();
    const observer = new ResizeObserver(place); observer.observe(element);
    return () => observer.disconnect();
  }, [anchor, placement]);
  return createPortal(<div ref={ref} class="ui-tip" role="tooltip" style={{ left: position?.left ?? anchor.left, top: position?.top ?? anchor.top, visibility: position ? "visible" : "hidden" }}>{children}</div>, document.body);
}

export function Tooltip({ content, children, placement = "top", class: cls = "", ideaId }: {
  content: ComponentChildren; children: ComponentChildren; placement?: "top" | "bottom"; class?: string; ideaId?: number;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const hover = useHoverPreview<boolean>({ ideaId: () => ideaId ?? null });
  const show = () => { if (ref.current) hover.show(true, ref.current.getBoundingClientRect()); };
  if (content == null || content === false) return <>{children}</>;
  return <span ref={ref} class={`ui-tip-trigger ${cls}`} onMouseEnter={show} onMouseMove={show} onMouseLeave={hover.clear} onFocusIn={show} onFocusOut={hover.clear}>
    {children}{hover.preview && <HoverCard anchor={hover.preview.anchor} placement={placement}>{content}</HoverCard>}
  </span>;
}
