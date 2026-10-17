/**
 * Filter picker for the chart toolbar: choose a saved filter (My filters /
 * Shared), type an ad-hoc expression with live validation, save it, share,
 * copy, edit or remove filters. The selection applies to all plots, the
 * table and idea means (state/filters.ts).
 */
import { useEffect, useRef, useState } from "preact/hooks";
import {
  activeFilter, activeSelection, archiveFilter, checkFilter, copyFilter, createFilter,
  myHumanId, savedFilters, selectionError, selectionLabels, updateFilter, type FilterCheck, type SavedFilter,
} from "../state/filters";
import { allExperiments } from "../state/signals";
import { showCancelled } from "../state/settings";
import { FilterExprInput } from "./filter-expr-input";

const EXAMPLES = [
  "meta.MAX_WALL_SECONDS >= 9000",
  "idea under 1169 and status = completed",
  "metric.score > 20 and tag != smoke",
  "description ~ sampling",
];

export function FilterPicker() {
  const [open, setOpen] = useState(false);
  const sel = activeSelection.value;
  const current = activeFilter.value;
  const count = selectionLabels.value?.size;
  const total = allExperiments.value.length;
  const label = current
    ? `${current.name}${sel.where.trim() ? " + expr" : ""}`
    : sel.where.trim() ? "expression" : "Filter";
  const root = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", close, true);
    return () => document.removeEventListener("pointerdown", close, true);
  }, [open]);
  return (
    <span class="filter-picker" ref={root}>
      <button
        type="button"
        class={`ui-toggle${current || sel.where.trim() ? " is-active" : ""}${selectionError.value ? " is-error" : ""}`}
        onClick={() => setOpen(!open)}
        title={selectionError.value ? `Filter error: ${selectionError.value}`
          : current || sel.where.trim() ? `${count ?? "…"} of ${total} experiments selected — click to change`
          : "Select experiments by a saved filter or an expression (applies to all plots, the table and idea means)"}
      >
        <svg class="filter-funnel" width="13" height="13" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 3h12l-4.5 5.5V13l-3 1.5V8.5Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" /></svg>
        <span class="filter-picker-label">{label}{(current || sel.where.trim()) && count !== undefined ? ` (${count})` : ""}</span>
      </button>
      {open && <FilterPanel onClose={() => setOpen(false)} />}
    </span>
  );
}

function FilterPanel({ onClose }: { onClose: () => void }) {
  const sel = activeSelection.value;
  const me = myHumanId();
  const filters = savedFilters.value;
  const mine = filters.filter((f) => f.owner === me);
  const shared = filters.filter((f) => f.shared && f.owner !== me);
  const [draft, setDraft] = useState(sel.where);
  const [check, setCheck] = useState<FilterCheck | null>(null);
  const [saveName, setSaveName] = useState("");
  const [saveShared, setSaveShared] = useState(false);
  const [editing, setEditing] = useState<SavedFilter | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // Live validation of the expression (combined with the chosen filter).
  useEffect(() => {
    const t = window.setTimeout(() => {
      checkFilter(draft, editing ? null : sel.filter).then(setCheck).catch((e) => setCheck({ ok: false, error: String(e.message || e), matched: 0, total: null, sample: [] }));
    }, 250);
    return () => window.clearTimeout(t);
  }, [draft, sel.filter, editing?.id]);

  const apply = (filter: string | null, where: string) => { activeSelection.value = { filter, where }; };
  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setMsg(null);
    try { await fn(); setMsg({ ok: true, text: ok }); } catch (e) { setMsg({ ok: false, text: String((e as Error).message || e) }); }
  };

  const row = (f: SavedFilter) => {
    const active = sel.filter === f.id;
    const own = f.owner === me;
    return (
      <li key={f.id} class={`filter-row${active ? " is-active" : ""}`}>
        <button type="button" class="filter-row-main" onClick={() => apply(active ? null : f.id, sel.where)}
          title={`${f.expr || "(all experiments)"}${f.exclude.length ? `\nexcluding ${f.exclude.length}: ${f.exclude.slice(0, 12).join(", ")}${f.exclude.length > 12 ? " …" : ""}` : ""}${f.description ? `\n${f.description}` : ""}`}>
          <span class="filter-row-name">{active ? "● " : ""}{f.name}</span>
          <code class="filter-row-expr">{f.expr || "all"}{f.exclude.length ? ` − ${f.exclude.length}` : ""}</code>
          {own && f.shared && <span class="filter-row-badge" title="Shared with everyone">shared</span>}
          {!own && <span class="filter-row-owner">{f.owner.replace(/^user:/, "")}</span>}
        </button>
        <span class="filter-row-actions">
          {own ? (
            <>
              <button type="button" title="Edit expression" onClick={() => { setEditing(f); setDraft(f.expr); setMsg(null); }}>edit</button>
              <button type="button" title={f.shared ? "Make personal" : "Share with everyone"}
                onClick={() => run(() => updateFilter(f.id, { shared: !f.shared }), f.shared ? "now personal" : "shared")}>
                {f.shared ? "unshare" : "share"}
              </button>
              <button type="button" title="Remove this filter"
                onClick={() => { if (confirm(`Remove filter "${f.name}"?`)) run(() => archiveFilter(f.id), "removed"); }}>✕</button>
            </>
          ) : (
            <button type="button" title="Copy into my filters to edit it"
              onClick={() => { const n = prompt("Name for your copy", `${f.name} (copy)`); if (n) run(() => copyFilter(f.id, n), `copied as ${n}`); }}>copy</button>
          )}
        </span>
      </li>
    );
  };

  return (
    <div class="filter-panel" role="dialog" aria-label="Experiment filter">
      <div class="filter-panel-head">
        <strong>Filter experiments</strong>
        <span class="filter-panel-hint">applies to all plots, the table and idea means</span>
        <label class="filter-cancelled" title="Cancelled experiments in the idea panel and the Recent list (charts never include them)">
          <input type="checkbox" checked={showCancelled.value} onChange={(e) => { showCancelled.value = (e.target as HTMLInputElement).checked; }} /> show cancelled
        </label>
        <button type="button" class="filter-close" onClick={onClose} aria-label="Close">×</button>
      </div>
      {mine.length > 0 && <div class="filter-group"><div class="filter-group-title">My filters</div><ul>{mine.map(row)}</ul></div>}
      {shared.length > 0 && <div class="filter-group"><div class="filter-group-title">Shared</div><ul>{shared.map(row)}</ul></div>}
      {!mine.length && !shared.length && <div class="filter-empty">No saved filters yet. Write an expression below and save it.</div>}

      <div class="filter-expr">
        <div class="filter-group-title">{editing ? `Edit “${editing.name}”` : sel.filter ? "Expression (combined with the filter above)" : "Expression"}</div>
        <FilterExprInput value={draft} onChange={setDraft} placeholder={EXAMPLES[0]}
          onSubmit={() => { if (!editing && check?.ok) apply(sel.filter, draft); }} />
        <div class={`filter-check${check && !check.ok ? " is-error" : ""}`}>
          {!check ? "…" : check.ok ? `✓ ${check.matched} of ${check.total} experiments` : `✗ ${check.error}`}
        </div>
        <div class="filter-actions">
          {editing ? (
            <>
              <button type="button" disabled={!check?.ok}
                onClick={() => run(async () => { await updateFilter(editing.id, { expr: draft }); setEditing(null); setDraft(sel.where); }, "filter updated")}>Save changes</button>
              <button type="button" onClick={() => { setEditing(null); setDraft(sel.where); }}>Cancel</button>
            </>
          ) : (
            <>
              <button type="button" disabled={!check?.ok || draft === sel.where} onClick={() => apply(sel.filter, draft)}>Apply</button>
              <button type="button" disabled={!sel.filter && !sel.where} onClick={() => { apply(null, ""); setDraft(""); }}>Clear all</button>
            </>
          )}
        </div>
        {!editing && (
          <div class="filter-save">
            <input placeholder="name, e.g. long" value={saveName} maxLength={64}
              onInput={(e) => setSaveName((e.target as HTMLInputElement).value)} />
            <label><input type="checkbox" checked={saveShared} onChange={(e) => setSaveShared((e.target as HTMLInputElement).checked)} /> shared</label>
            <button type="button" disabled={!saveName.trim() || !check?.ok}
              title="Save the expression as a named filter"
              onClick={() => run(async () => {
                const rec = await createFilter({ name: saveName.trim(), expr: draft, shared: saveShared });
                setSaveName(""); apply(rec.id, ""); setDraft("");
              }, "saved")}>Save as filter</button>
          </div>
        )}
        {msg && <div class={msg.ok ? "filter-msg-ok" : "filter-msg-err"}>{msg.text}</div>}
        <details class="filter-help">
          <summary>Syntax</summary>
          <div>
            Fields: <code>meta.KEY</code>, <code>metric.KEY</code>, <code>tag</code>, <code>idea</code> (<code>idea under N</code>), <code>label</code>,
            <code>status</code>, <code>description</code>, <code>attempt</code>, <code>runtime</code>, <code>created_at</code>.
            Operators: <code>= != &gt; &gt;= &lt; &lt;=</code>, <code>~</code> contains, <code>in [a, b]</code>, <code>exists</code>, <code>missing</code>;
            combine with <code>AND</code> / <code>OR</code> / <code>NOT</code> (any case) and parentheses; <code>@name</code> uses a saved filter
            (spaces as <code>_</code>). Suggestions follow as you type: Tab or Enter takes one, arrows move.
            <ul>{EXAMPLES.map((x) => <li key={x}><button type="button" onClick={() => setDraft(x)}><code>{x}</code></button></li>)}</ul>
          </div>
        </details>
      </div>
    </div>
  );
}

/** "Exclude from «filter»" / "include again" for one experiment, shown while a
 *  saved filter is active. Owners edit their filter; on a shared filter any
 *  human may adjust its exclusions. */
export function ExcludeFromFilter({ label, compact = false, onDone }: { label: string; compact?: boolean; onDone?: () => void }) {
  const f = activeFilter.value;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!f || (f.owner !== myHumanId() && !f.shared)) return null;
  const excluded = f.exclude.includes(label);
  return (
    <button type="button" class={`exclude-toggle${excluded ? " is-excluded" : ""}${compact ? " is-compact" : ""}`} disabled={busy}
      title={err ?? (excluded ? `Excluded from filter “${f.name}” — click to include it again` : `Leave this experiment out of filter “${f.name}”`)}
      onClick={async (ev) => {
        ev.stopPropagation();
        setBusy(true); setErr(null);
        try {
          await updateFilter(f.id, excluded ? { remove_exclude: [label] } : { add_exclude: [label] });
          onDone?.();
        } catch (e) { setErr(String((e as Error).message || e)); }
        finally { setBusy(false); }
      }}>
      {err ? "⚠ " : ""}{excluded ? `include in “${f.name}”` : `exclude from “${f.name}”`}
    </button>
  );
}
