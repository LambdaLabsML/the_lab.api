/**
 * SearchFilter — a reusable, fully controlled search-with-pills filter.
 *
 * One text input drives a grouped suggestions dropdown; chosen suggestions
 * become category-colored pills below the input. The component owns no data —
 * consumers pass `applied` pills and a `suggest(query)` function and react to
 * the `onApply` / `onApplyText` / `onToggle` / `onRemove` callbacks. This keeps
 * it generic enough to drive both Review content filtering and idea-graph nav.
 *
 * Design language (see dashboard/DESIGN.md): hairline field, flat fills, one
 * accent, token type. The dropdown reuses the tooltip's fixed + viewport-clamped
 * positioning (components/ui/tooltip.tsx) so it never clips inside a scroll
 * container — measured pre-paint in a layout effect, flipped/nudged to fit.
 *
 *   <SearchFilter
 *     placeholder="Filter…"
 *     applied={pills}
 *     suggest={(q) => matches(q)}
 *     onApply={addPill}
 *     onApplyText={addFreeText}
 *     onToggle={togglePill}
 *     onRemove={removePill}
 *     categoryColor={(c) => COLORS[c]}
 *   />
 */
import { useState, useRef, useLayoutEffect, useMemo, useCallback } from "preact/hooks";
import type { JSX } from "preact";

export interface FilterItem {
  /** stable unique id, e.g. `${category}:${value}` */
  id: string;
  /** e.g. "idea" | "tag" | "experiment" | "text" */
  category: string;
  /** display text */
  label: string;
  /** underlying value */
  value: string;
}

export interface SearchFilterProps {
  placeholder?: string;
  /** current pills (controlled) — `active` defaults to true when undefined */
  applied: Array<FilterItem & { active?: boolean }>;
  /** consumer returns matches across categories for the current query */
  suggest: (query: string) => FilterItem[];
  /** add a pill (from a suggestion) */
  onApply: (item: FilterItem) => void;
  /** Enter on raw text with no highlighted suggestion → free-text filter */
  onApplyText?: (text: string) => void;
  /** toggle a pill active/inactive */
  onToggle: (id: string) => void;
  /** remove a pill */
  onRemove: (id: string) => void;
  /** optional: maps category → a CSS color/var for the pill accent + group eyebrow */
  categoryColor?: (category: string) => string;
  class?: string;
}

const MARGIN = 8; // min gap from viewport edge
const GAP = 4; // gap between input and dropdown

/** Case-insensitive match span for emphasizing the typed substring in a label. */
function matchSpan(label: string, query: string): [number, number] | null {
  if (!query) return null;
  const i = label.toLowerCase().indexOf(query.toLowerCase());
  return i < 0 ? null : [i, i + query.length];
}

/** Renders a label with the matched substring emphasized. */
function Highlight({ label, query }: { label: string; query: string }) {
  const span = matchSpan(label, query);
  if (!span) return <>{label}</>;
  const [a, b] = span;
  return (
    <>
      {label.slice(0, a)}
      <em class="sf-mark">{label.slice(a, b)}</em>
      {label.slice(b)}
    </>
  );
}

export function SearchFilter({
  placeholder = "Filter…",
  applied,
  suggest,
  onApply,
  onApplyText,
  onToggle,
  onRemove,
  categoryColor,
  class: cls = "",
}: SearchFilterProps): JSX.Element {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0); // highlighted flat index

  const fieldRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // Anchor rect (captured on open / on query change) + clamped position.
  const [anchor, setAnchor] = useState<{ left: number; top: number; bottom: number; width: number } | null>(null);
  const [pos, setPos] = useState<{ left: number; top: number; width: number } | null>(null);

  // Ids already applied — de-dupe suggestions defensively even if the consumer
  // also filters them out.
  const appliedIds = useMemo(() => new Set(applied.map((p) => p.id)), [applied]);

  // Flatten raw suggestions → de-duped, grouped-by-category, with a stable flat
  // index per row for keyboard navigation.
  const { groups, flat } = useMemo(() => {
    const raw = query.trim() ? suggest(query.trim()) : [];
    const seen = new Set<string>();
    const ordered: FilterItem[] = [];
    for (const it of raw) {
      if (appliedIds.has(it.id) || seen.has(it.id)) continue;
      seen.add(it.id);
      ordered.push(it);
    }
    // group preserving first-seen category order
    const byCat = new Map<string, FilterItem[]>();
    for (const it of ordered) {
      const arr = byCat.get(it.category);
      if (arr) arr.push(it);
      else byCat.set(it.category, [it]);
    }
    const flat: FilterItem[] = [];
    const groups: Array<{ category: string; items: Array<{ item: FilterItem; index: number }> }> = [];
    for (const [category, items] of byCat) {
      groups.push({
        category,
        items: items.map((item) => {
          const index = flat.length;
          flat.push(item);
          return { item, index };
        }),
      });
    }
    return { groups, flat };
  }, [query, suggest, appliedIds]);

  const showMenu = open && query.trim().length > 0 && flat.length > 0;

  const captureAnchor = useCallback(() => {
    const el = fieldRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setAnchor({ left: r.left, top: r.top, bottom: r.bottom, width: r.width });
    setPos(null);
  }, []);

  // Re-measure when the menu's contents or anchor change, and clamp into the
  // viewport (flip above the field when there isn't room below). Pre-paint.
  useLayoutEffect(() => {
    if (!showMenu) {
      setPos(null);
      return;
    }
    if (!anchor || !menuRef.current) return;
    const menu = menuRef.current.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    let top = anchor.bottom + GAP;
    // flip above if it would overflow the bottom and there's more room above
    if (top + menu.height > vh - MARGIN && anchor.top - menu.height - GAP >= MARGIN) {
      top = anchor.top - menu.height - GAP;
    }
    top = Math.max(MARGIN, Math.min(top, vh - menu.height - MARGIN));

    let left = anchor.left;
    left = Math.max(MARGIN, Math.min(left, vw - menu.width - MARGIN));

    setPos({ left, top, width: anchor.width });
    // depend on the rendered rows so it re-clamps as the list size changes
  }, [showMenu, anchor, flat.length, groups.length]);

  const openWith = useCallback(
    (q: string) => {
      setQuery(q);
      setActive(0);
      if (q.trim()) {
        setOpen(true);
        captureAnchor();
      } else {
        setOpen(false);
      }
    },
    [captureAnchor],
  );

  const close = useCallback(() => {
    setOpen(false);
    setPos(null);
  }, []);

  const apply = useCallback(
    (item: FilterItem) => {
      onApply(item);
      setQuery("");
      close();
      inputRef.current?.focus();
    },
    [onApply, close],
  );

  const applyText = useCallback(() => {
    const t = query.trim();
    if (!t || !onApplyText) return;
    onApplyText(t);
    setQuery("");
    close();
    inputRef.current?.focus();
  }, [query, onApplyText, close]);

  const onKeyDown = useCallback(
    (e: JSX.TargetedKeyboardEvent<HTMLInputElement>) => {
      if (e.key === "ArrowDown") {
        if (!showMenu) return;
        e.preventDefault();
        setActive((i) => (flat.length ? (i + 1) % flat.length : 0));
      } else if (e.key === "ArrowUp") {
        if (!showMenu) return;
        e.preventDefault();
        setActive((i) => (flat.length ? (i - 1 + flat.length) % flat.length : 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        const hit = showMenu ? flat[active] : undefined;
        if (hit) apply(hit);
        else applyText();
      } else if (e.key === "Escape") {
        if (showMenu) {
          e.preventDefault();
          close();
        }
      }
    },
    [showMenu, flat, active, apply, applyText, close],
  );

  // Keep the active row visible as the highlight moves.
  useLayoutEffect(() => {
    if (!showMenu || !menuRef.current) return;
    const row = menuRef.current.querySelector<HTMLElement>(`[data-sf-index="${active}"]`);
    row?.scrollIntoView({ block: "nearest" });
  }, [active, showMenu]);

  const q = query.trim();
  const hasPills = applied.length > 0;

  return (
    <div class={`sf ${cls}`} ref={fieldRef}>
      <div class="sf-field">
        <svg class="sf-icon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
          <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.4" />
          <line x1="10.5" y1="10.5" x2="14" y2="14" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
        </svg>
        <input
          ref={inputRef}
          class="sf-input"
          type="text"
          value={query}
          placeholder={placeholder}
          spellcheck={false}
          autocomplete="off"
          aria-autocomplete="list"
          aria-expanded={showMenu}
          onInput={(e) => openWith((e.target as HTMLInputElement).value)}
          onFocus={() => {
            if (query.trim()) {
              setOpen(true);
              captureAnchor();
            }
          }}
          onBlur={() => {
            // delay so a mousedown on a row resolves before we close
            window.setTimeout(close, 120);
          }}
          onKeyDown={onKeyDown}
        />
        {query && (
          <button
            type="button"
            class="sf-input-clear"
            title="Clear text"
            aria-label="Clear text"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              setQuery("");
              close();
              inputRef.current?.focus();
            }}
          >
            ×
          </button>
        )}
        {hasPills && (
          <button
            type="button"
            class="sf-clear-all"
            title="Clear all filters"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              for (const p of applied) onRemove(p.id);
            }}
          >
            Clear all
          </button>
        )}
      </div>

      {hasPills && (
        <div class="sf-pills" role="list">
          {applied.map((p) => {
            const isActive = p.active !== false;
            const color = categoryColor?.(p.category);
            return (
              <span
                key={p.id}
                role="listitem"
                class={`sf-pill${isActive ? "" : " is-off"}`}
                title={`${p.category}: ${p.label}${isActive ? "" : " (off)"}`}
                style={color ? ({ "--sf-pill-accent": color } as JSX.CSSProperties) : undefined}
              >
                <button
                  type="button"
                  class="sf-pill-body"
                  aria-pressed={isActive}
                  onClick={() => onToggle(p.id)}
                >
                  <span class="sf-pill-dot" aria-hidden="true" />
                  <span class="sf-pill-label">{p.label}</span>
                </button>
                <button
                  type="button"
                  class="sf-pill-x"
                  title="Remove filter"
                  aria-label={`Remove ${p.label}`}
                  onClick={() => onRemove(p.id)}
                >
                  ×
                </button>
              </span>
            );
          })}
        </div>
      )}

      {showMenu && (
        <div
          ref={menuRef}
          class="sf-menu"
          role="listbox"
          style={{
            left: `${pos ? pos.left : anchor?.left ?? 0}px`,
            top: `${pos ? pos.top : anchor?.bottom ?? 0}px`,
            width: `${pos ? pos.width : anchor?.width ?? 0}px`,
            visibility: pos ? "visible" : "hidden",
          }}
          // keep focus on the input while interacting with the menu
          onMouseDown={(e) => e.preventDefault()}
        >
          {groups.map((g) => {
            const color = categoryColor?.(g.category);
            return (
              <div class="sf-group" key={g.category}>
                <div
                  class="sf-group-eyebrow"
                  style={color ? ({ "--sf-pill-accent": color } as JSX.CSSProperties) : undefined}
                >
                  <span class="sf-group-dot" aria-hidden="true" />
                  {g.category}
                </div>
                {g.items.map(({ item, index }) => (
                  <div
                    key={item.id}
                    data-sf-index={index}
                    role="option"
                    aria-selected={index === active}
                    class={`sf-option${index === active ? " is-active" : ""}`}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => apply(item)}
                  >
                    <Highlight label={item.label} query={q} />
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
