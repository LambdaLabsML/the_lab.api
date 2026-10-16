import { signal } from "@preact/signals";
import { useEffect, useRef, useState } from "preact/hooks";
import { databaseScope, databaseTab } from "../state/database";
import { allExperiments, highlightedIdea } from "../state/signals";
import { isHiddenExperiment, setExperimentHidden } from "../state/hidden";
import { serverDemoMode } from "../state/settings";
import { navigateToIdea } from "../lib/navigate";

export type ExperimentTarget = { ideaId: number; label: string };
const surfaces = new WeakMap<Element, (event: MouseEvent) => ExperimentTarget | null>();
const menu = signal<(ExperimentTarget & { x: number; y: number }) | null>(null);
export function registerExperimentSurface(element: Element, resolve: (event: MouseEvent) => ExperimentTarget | null) {
  surfaces.set(element, resolve);
  return () => { surfaces.delete(element); };
}

export function ResearchIcon({ name }: { name: "search" | "graph" | "table" | "info" | "question" | "reply" | "hide" | "show" }) {
  const paths = {
    search: <><circle cx="10" cy="10" r="6" /><path d="m15 15 5 5" /></>,
    graph: <><circle cx="5" cy="12" r="2" /><circle cx="18" cy="5" r="2" /><circle cx="18" cy="19" r="2" /><path d="m7 11 9-5M7 13l9 5" /></>,
    table: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 9h18M3 14h18M9 4v16" /></>,
    info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 7v1" /></>,
    question: <><path d="M8 8a4 4 0 1 1 6 3.5c-2 1-2 1-2 3M12 18v1" /></>,
    show: <><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7S2 12 2 12Z" /><circle cx="12" cy="12" r="3" /></>,
    hide: <><path d="m3 3 18 18M10 5c7-1 12 7 12 7a20 20 0 0 1-4 5M6 6a20 20 0 0 0-4 6s3 7 10 7c2 0 3-1 4-1M10 10a3 3 0 0 0 4 4" /></>,
    reply: <><path d="m9 5-6 6 6 6M3 11h10a7 7 0 0 1 7 7" /></>,
  };
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

/** One delegated menu for SVG points, experiment links/rows, and canvas hits. */
export function ExperimentActions() {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let start: { x: number; y: number; pointer: number } | null = null;
    let suppressUntil = 0;
    const cancel = () => { if (timer) clearTimeout(timer); timer = null; start = null; };
    const resolve = (event: MouseEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      const entity = target?.closest<HTMLElement>("[data-experiment-label]");
      if (entity) {
        const ideaId = Number(entity.dataset.ideaId);
        const label = entity.dataset.experimentLabel;
        if (ideaId > 0 && label) return { ideaId, label };
      }
      const canvas = target?.closest("canvas");
      return canvas ? surfaces.get(canvas)?.(event) ?? null : null;
    };
    const open = (event: MouseEvent, target: ExperimentTarget) => {
      returnFocus.current = document.activeElement as HTMLElement;
      menu.value = { ...target, x: Math.max(8, Math.min(innerWidth - 214, event.clientX)), y: Math.max(8, Math.min(innerHeight - 92, event.clientY)) };
      suppressUntil = Date.now() + 800;
    };
    const context = (e: MouseEvent) => {
      const target = resolve(e);
      if (!target) return;
      e.preventDefault(); cancel(); open(e, target);
    };
    const down = (e: PointerEvent) => {
      cancel();
      if (e.button !== 0 || root.current?.contains(e.target as Node)) return;
      const target = resolve(e);
      if (!target) return;
      start = { x: e.clientX, y: e.clientY, pointer: e.pointerId };
      timer = setTimeout(() => { open(e, target); timer = null; }, 500);
    };
    const move = (e: PointerEvent) => { if (start && (e.pointerId !== start.pointer || Math.hypot(e.clientX - start.x, e.clientY - start.y) > 10)) cancel(); };
    const click = (e: MouseEvent) => {
      if (menu.value && Date.now() < suppressUntil && !root.current?.contains(e.target as Node) && !(e.target instanceof Element && e.target.closest(".experiment-menu-scrim"))) { e.preventDefault(); e.stopPropagation(); }
    };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape" && menu.value) { menu.value = null; returnFocus.current?.focus(); } };
    document.addEventListener("contextmenu", context);
    document.addEventListener("pointerdown", down, true);
    document.addEventListener("pointermove", move, true);
    document.addEventListener("pointerup", cancel, true);
    document.addEventListener("pointercancel", cancel, true);
    document.addEventListener("click", click, true);
    document.addEventListener("keydown", key);
    window.addEventListener("scroll", cancel, true);
    return () => {
      cancel(); document.removeEventListener("contextmenu", context); document.removeEventListener("pointerdown", down, true);
      document.removeEventListener("pointermove", move, true); document.removeEventListener("pointerup", cancel, true);
      document.removeEventListener("pointercancel", cancel, true); document.removeEventListener("click", click, true);
      document.removeEventListener("keydown", key); window.removeEventListener("scroll", cancel, true);
    };
  }, []);
  const target = menu.value;
  useEffect(() => { setError(null); if (target) root.current?.querySelector<HTMLButtonElement>("button")?.focus(); }, [target]);
  if (!target) return null;
  const experiment = allExperiments.value.find(exp => String(exp.label ?? exp.id) === target.label);
  const hidden = !!experiment && isHiddenExperiment(experiment);
  async function toggleHidden() {
    if (!target || saving) return;
    setSaving(true); setError(null);
    try { await setExperimentHidden(target.label, !hidden); menu.value = null; }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setSaving(false); }
  }
  function choose(destination: "graph" | "experiments" | "info") {
    if (!target) return;
    menu.value = null;
    if (destination === "info") { navigateToIdea(target.ideaId, target.label); return; }
    databaseTab.value = destination;
    databaseScope.value = { ideaId: target.ideaId, label: target.label };
    highlightedIdea.value = target.ideaId;
    window.dispatchEvent(new CustomEvent("the-lab:open-database"));
  }
  return <>
    <button class="experiment-menu-scrim" aria-label="Dismiss experiment actions" onClick={() => { menu.value = null; returnFocus.current?.focus(); }} />
    <div ref={root} class="experiment-action-menu" role="menu" aria-label={`Actions for experiment ${target.label}`} style={{ left: target.x, top: target.y }} onKeyDown={e => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      e.preventDefault();
      const buttons = [...root.current!.querySelectorAll<HTMLButtonElement>("button")];
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      buttons[e.key === "Home" ? 0 : e.key === "End" ? buttons.length - 1 : (index + (e.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length]?.focus();
    }}>
      <span class="experiment-menu-caption">exp/{target.label}</span>
      <div>{([["graph", "Show idea graph"], ["experiments", "Show experiment in Database"], ["info", "Open idea and experiment details"]] as const).map(([destination, title]) => <button class="ui-btn" role="menuitem" title={title} aria-label={title} onClick={() => choose(destination)}><ResearchIcon name={destination === "experiments" ? "table" : destination} /></button>)}<button class="ui-btn" role="menuitem" title={hidden ? "Unhide experiment" : "Hide experiment"} aria-label={hidden ? "Unhide experiment" : "Hide experiment"} disabled={saving || serverDemoMode.value} onClick={toggleHidden}><ResearchIcon name={hidden ? "show" : "hide"} /></button></div>
      {error && <span class="experiment-menu-error" role="alert">{error}</span>}
    </div>
  </>;
}
