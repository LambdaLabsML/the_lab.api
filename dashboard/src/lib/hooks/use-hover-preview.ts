/** One hover lifecycle for SVG points, canvas hits, and ordinary controls.
 * Clicks/layout changes dismiss every card; only a real pointer move (not a
 * chart's replayed mouse event) can show one again. Keyboard focus still works.
 */
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { highlightedIdea } from "../../state/signals";

export const POINT_HIT_RADIUS = 12;

type Point = { x: number; y: number };
const active = new Map<object, () => void>();
let pointer: Point | null = null;
let blockedAt: Point | null = null;
let blocked = false;
let users = 0;

/** Also lets richer previews (such as the monitor's live log card) share
 * dismissal without replacing their content or fetching behavior. */
export function claimHoverPreview(owner: object, close: () => void): boolean {
  if (blocked) return false;
  for (const [other, dismiss] of [...active]) if (other !== owner) dismiss();
  active.set(owner, close);
  return true;
}
export function releaseHoverPreview(owner: object) { active.delete(owner); }

export function dismissHoverPreviews() {
  blocked = true;
  blockedAt = pointer;
  for (const close of [...active.values()]) close();
}
const down = (incoming: Event) => {
  const event = incoming as PointerEvent;
  pointer = { x: event.clientX, y: event.clientY };
  dismissHoverPreviews();
};
const move = (incoming: Event) => {
  const event = incoming as PointerEvent;
  if (event.pointerType === "touch") return;
  pointer = { x: event.clientX, y: event.clientY };
  if (blocked && (!blockedAt || Math.hypot(pointer.x - blockedAt.x, pointer.y - blockedAt.y) >= 4)) blocked = false;
};
const key = (incoming: Event) => {
  const event = incoming as KeyboardEvent;
  if (event.key === "Escape") {
    const hadPreview = active.size > 0;
    dismissHoverPreviews();
    if (hadPreview) event.stopPropagation();
  }
  if (event.key === "Tab") blocked = false;
};
const events = ["scroll", "resize"] as const;
function listen(add: boolean) {
  const method = add ? "addEventListener" : "removeEventListener";
  document[method]("pointerdown", down, true);
  document[method]("pointermove", move, true);
  document[method]("pointercancel", dismissHoverPreviews, true);
  document[method]("contextmenu", dismissHoverPreviews, true);
  document[method]("click", dismissHoverPreviews, true);
  window[method]("blur", dismissHoverPreviews);
  document[method]("keydown", key, true);
  document[method]("visibilitychange", dismissHoverPreviews);
  for (const event of events) window[method](event, dismissHoverPreviews, true);
}

export function useHoverPreview<T>(options: { ideaId?: (value: T) => number | null; key?: (value: T) => unknown } = {}) {
  const owner = useRef({});
  const [preview, setPreview] = useState<{ value: T; anchor: DOMRect } | null>(null);
  const current = useRef(preview);
  const config = useRef(options); config.current = options;
  const clear = useCallback(() => {
    active.delete(owner.current);
    const previous = current.current;
    current.current = null;
    if (previous) {
      const id = config.current.ideaId?.(previous.value);
      if (id != null && highlightedIdea.value === id) highlightedIdea.value = null;
      setPreview(null);
    }
  }, []);
  useEffect(() => {
    if (++users === 1) listen(true);
    return () => { clear(); if (--users === 0) { listen(false); blocked = false; pointer = blockedAt = null; } };
  }, []);
  const show = useCallback((value: T, anchor: DOMRect) => {
    if (!claimHoverPreview(owner.current, clear)) return;
    const previous = current.current;
    const same = previous && (config.current.key ? config.current.key(previous.value) === config.current.key(value) : previous.value === value);
    const id = config.current.ideaId?.(value);
    const oldId = previous && config.current.ideaId?.(previous.value);
    if (oldId != null && oldId !== id && highlightedIdea.value === oldId) highlightedIdea.value = null;
    if (id != null) highlightedIdea.value = id;
    if (previous && same && previous.anchor.left === anchor.left && previous.anchor.top === anchor.top && previous.anchor.width === anchor.width && previous.anchor.height === anchor.height) return;
    const next = { value, anchor };
    current.current = next;
    setPreview(next);
  }, []);
  return { preview, show, clear };
}
