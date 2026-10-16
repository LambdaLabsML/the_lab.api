/**
 * Filter expression input with context-aware autocomplete (fields, operators,
 * values, AND/OR/NOT) and quick-insert buttons, including a searchable tag
 * picker. Vocabulary (meta / metric keys and their common values, tags,
 * statuses, saved filter names) comes from GET /api/v1/filters/vocabulary.
 */
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { signal } from "@preact/signals";

interface Vocabulary {
  meta: { key: string; count: number; values: string[] }[];
  metrics: { key: string; count: number }[];
  tags: { tag: string; count: number }[];
  statuses: string[];
  saved: string[];
}

const vocab = signal<Vocabulary | null>(null);
let vocabLoadedAt = 0;
async function loadVocabulary(): Promise<void> {
  if (Date.now() - vocabLoadedAt < 60_000 && vocab.value) return;
  vocabLoadedAt = Date.now();
  try {
    const r = await fetch("/api/v1/filters/vocabulary", { headers: { "X-The-Lab-Source": "dashboard" } });
    if (r.ok) vocab.value = await r.json();
  } catch { /* suggestions are optional */ }
}

type Suggestion = { text: string; insert: string; hint?: string; kind: "field" | "op" | "value" | "logic" | "ref" };

const BASE_FIELDS: [string, string][] = [
  ["tag", "has tag"], ["idea", "idea id (idea under N)"], ["status", ""], ["label", "e.g. 1050.1"],
  ["description", "use ~ for contains"], ["attempt", ""], ["runtime", "seconds"],
  ["created_at", ""], ["started_at", ""], ["finished_at", ""],
];
const OPS: [string, string][] = [
  ["=", ""], ["!=", ""], [">=", ""], [">", ""], ["<=", ""], ["<", ""],
  ["~", "contains"], ["!~", "doesn't contain"], ["in [", "one of"], ["not in [", "none of"],
  ["exists", ""], ["missing", ""],
];
const FIELD_RE = /^(tags?|idea|status|label|description|attempt|runtime|created_at|started_at|finished_at|id|meta\.\S+|metrics?\.\S+)$/i;
const OP_RE = /^(=|==|!=|>=|<=|>|<|~|!~)$/;

/** Light tokenizer for the text before the cursor. */
function tokens(text: string): string[] {
  const out: string[] = [];
  const re = /\s*("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|==|!=|>=|<=|!~|&&|\|\||[=<>~!()\[\],]|@[\w.:-]*|[\w.:/+*-]+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) out.push(m[1]);
  return out;
}

function quote(v: string): string {
  return /^[\w.:/+-]+$/.test(v) ? v : JSON.stringify(v);
}

/** Suggestions for the cursor position: what may come next, filtered by the
 *  word being typed. Returns the partial word's start so it can be replaced. */
function suggest(text: string, cursor: number, v: Vocabulary | null): { from: number; items: Suggestion[] } {
  const before = text.slice(0, cursor);
  const partialMatch = /[\w.:@/+-]*$/.exec(before);
  const partial = partialMatch ? partialMatch[0] : "";
  const from = cursor - partial.length;
  const toks = tokens(before.slice(0, from));
  const last = toks[toks.length - 1]?.toLowerCase();
  const p = partial.toLowerCase();
  const items: Suggestion[] = [];
  const add = (s: Suggestion) => { if (!p || s.text.toLowerCase().includes(p)) items.push(s); };

  const fieldBefore = (offset: number) => toks[toks.length - offset] ?? "";
  // Inside an "in [ ... ]" list → values of the field before "in".
  const lastOpen = toks.lastIndexOf("[");
  const inList = lastOpen >= 0 && !toks.slice(lastOpen).includes("]") && /^in$/i.test(toks[lastOpen - 1] ?? "");
  const valuesFor = (field: string) => {
    const f = field.toLowerCase();
    if (f === "tag" || f === "tags") v?.tags.forEach(t => add({ text: t.tag, insert: quote(t.tag), hint: `${t.count}`, kind: "value" }));
    else if (f === "status") (v?.statuses ?? ["completed", "running", "failed", "cancelled", "queued"]).forEach(s => add({ text: s, insert: s, kind: "value" }));
    else if (f.startsWith("meta.")) {
      const key = field.slice(5);
      const m = v?.meta.find(x => x.key === key) ?? v?.meta.find(x => x.key.toLowerCase() === key.toLowerCase());
      m?.values.forEach(val => add({ text: val, insert: quote(val), kind: "value" }));
    } else if (f === "true" || f === "false") { /* n/a */ }
  };

  if (inList) {
    const field = toks[lastOpen - 2] ?? "";
    valuesFor(field);
  } else if (!last || last === "(" || last === "and" || last === "or" || last === "not" || last === "&&" || last === "||" || last === "!") {
    // Expecting a condition.
    for (const [f, hint] of BASE_FIELDS) add({ text: f, insert: f + " ", hint, kind: "field" });
    v?.meta.forEach(m => add({ text: `meta.${m.key}`, insert: `meta.${m.key} `, hint: `${m.count}`, kind: "field" }));
    v?.metrics.forEach(m => add({ text: `metric.${m.key}`, insert: `metric.${m.key} `, hint: `${m.count}`, kind: "field" }));
    v?.saved.forEach(n => add({ text: `@${n.replace(/\s+/g, "_")}`, insert: `@${n.replace(/\s+/g, "_")} `, hint: "saved filter", kind: "ref" }));
    if (last !== "not") add({ text: "NOT", insert: "NOT ", kind: "logic" });
    add({ text: "(", insert: "(", kind: "logic" });
  } else if (FIELD_RE.test(toks[toks.length - 1] ?? "")) {
    // After a field → operator.
    const field = (toks[toks.length - 1] ?? "").toLowerCase();
    if (field === "idea") add({ text: "under", insert: "under ", hint: "idea and its sub-ideas", kind: "op" });
    for (const [op, hint] of OPS) add({ text: op, insert: op === "exists" || op === "missing" ? `${op} ` : `${op} `, hint, kind: "op" });
  } else if (OP_RE.test(toks[toks.length - 1] ?? "")) {
    valuesFor(fieldBefore(2));
  } else if (last === "in") {
    add({ text: "[", insert: "[", kind: "op" });
  } else {
    // After a complete condition → combinators.
    add({ text: "AND", insert: "AND ", kind: "logic" });
    add({ text: "OR", insert: "OR ", kind: "logic" });
    const opens = toks.filter(t => t === "(").length - toks.filter(t => t === ")").length;
    if (opens > 0) add({ text: ")", insert: ") ", kind: "logic" });
  }
  // Prefer prefix matches, then shorter.
  items.sort((a, b) => {
    const ap = a.text.toLowerCase().startsWith(p) ? 0 : 1, bp = b.text.toLowerCase().startsWith(p) ? 0 : 1;
    return ap - bp;
  });
  return { from, items: items.slice(0, 40) };
}

export function FilterExprInput({ value, onChange, onSubmit, placeholder }: {
  value: string;
  onChange: (v: string) => void;
  onSubmit?: () => void;
  placeholder?: string;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [cursor, setCursor] = useState(value.length);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  // Enter accepts a suggestion only after arrowing through the list or while
  // typing a word; otherwise it applies the filter. Tab always accepts.
  const [navigated, setNavigated] = useState(false);
  const [tagPicker, setTagPicker] = useState(false);
  const [tagQuery, setTagQuery] = useState("");
  useEffect(() => { void loadVocabulary(); }, []);
  const v = vocab.value;
  const { from, items } = useMemo(() => suggest(value, cursor, v), [value, cursor, v]);
  useEffect(() => { setActive(0); setNavigated(false); }, [value, cursor]);

  const syncCursor = () => setCursor(ref.current?.selectionStart ?? value.length);

  /** Replace text[from..cursor] with *insert*, adding spaces where needed. */
  const insertAt = (insert: string, start = from, end = cursor) => {
    const beforeText = value.slice(0, start);
    const after = value.slice(end);
    const pad = beforeText && !/[\s(\[]$/.test(beforeText) && !/^[)\],]/.test(insert) ? " " : "";
    const next = beforeText + pad + insert + after;
    const pos = (beforeText + pad + insert).length;
    onChange(next);
    requestAnimationFrame(() => {
      const el = ref.current;
      if (el) { el.focus(); el.setSelectionRange(pos, pos); }
      setCursor(pos);
      setOpen(true);
    });
  };
  const accept = (s: Suggestion) => insertAt(s.insert);
  const quick = (text: string) => {
    const el = ref.current;
    const s = el?.selectionStart ?? value.length, e = el?.selectionEnd ?? value.length;
    if (text === "()") {
      // Wrap the selection (or the whole expression when nothing is selected).
      if (s !== e) { onChange(value.slice(0, s) + "(" + value.slice(s, e) + ")" + value.slice(e)); return; }
      if (value.trim()) { onChange(`(${value.trim()}) `); requestAnimationFrame(() => { ref.current?.focus(); setCursor(value.trim().length + 3); }); return; }
      insertAt("(", s, e);
      return;
    }
    insertAt(text, s, e);
  };

  const tags = (v?.tags ?? []).filter(t => !tagQuery || t.tag.toLowerCase().includes(tagQuery.toLowerCase())).slice(0, 60);

  return (
    <div class="filter-expr-input">
      <div class="filter-quick" role="toolbar" aria-label="Insert into the expression">
        <button type="button" onClick={() => quick("AND ")} title="Both conditions">AND</button>
        <button type="button" onClick={() => quick("OR ")} title="Either condition">OR</button>
        <button type="button" onClick={() => quick("NOT ")} title="Negate the next condition">NOT</button>
        <button type="button" onClick={() => quick("()")} title="Parentheses (wraps the selection, or the whole expression)">( )</button>
        <span class="filter-tag-picker">
          <button type="button" class={tagPicker ? "is-open" : ""} onClick={() => { setTagPicker(!tagPicker); void loadVocabulary(); }} title="Pick a tag">tag ▾</button>
          {tagPicker && (
            <div class="filter-tag-menu">
              <input autoFocus placeholder="search tags…" value={tagQuery}
                onInput={(e) => setTagQuery((e.target as HTMLInputElement).value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setTagPicker(false);
                  if (e.key === "Enter" && tags[0]) { e.preventDefault(); quick(`tag = ${quote(tags[0].tag)} `); setTagPicker(false); setTagQuery(""); }
                }} />
              <ul>
                {tags.map(t => (
                  <li key={t.tag}>
                    <button type="button" onClick={() => { quick(`tag = ${quote(t.tag)} `); setTagPicker(false); setTagQuery(""); }}>
                      <span>{t.tag}</span><span class="filter-sugg-hint">{t.count}</span>
                    </button>
                    <button type="button" class="filter-tag-not" title={`tag != ${t.tag}`}
                      onClick={() => { quick(`tag != ${quote(t.tag)} `); setTagPicker(false); setTagQuery(""); }}>≠</button>
                  </li>
                ))}
                {!tags.length && <li class="filter-sugg-empty">{v ? "no matching tags" : "loading…"}</li>}
              </ul>
            </div>
          )}
        </span>
      </div>
      <div class="filter-expr-box">
        <textarea
          ref={ref}
          rows={2}
          value={value}
          spellcheck={false}
          placeholder={placeholder}
          aria-autocomplete="list"
          onInput={(e) => { onChange((e.target as HTMLTextAreaElement).value); syncCursor(); setOpen(true); }}
          onClick={() => { syncCursor(); setOpen(true); }}
          onKeyUp={(e) => { if (!["ArrowUp", "ArrowDown", "Enter", "Tab", "Escape"].includes(e.key)) syncCursor(); }}
          onFocus={() => { syncCursor(); setOpen(true); }}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          onKeyDown={(e) => {
            const showing = open && items.length > 0;
            if (showing && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
              e.preventDefault();
              setActive((active + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length);
              setNavigated(true);
              return;
            }
            const typingWord = from < cursor;
            if (showing && (e.key === "Tab" || (e.key === "Enter" && !e.ctrlKey && !e.metaKey && (navigated || typingWord)))) {
              e.preventDefault();
              accept(items[active]);
              return;
            }
            if (e.key === "Escape") { setOpen(false); return; }
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSubmit?.(); }
          }}
        />
        {open && items.length > 0 && (
          <ul class="filter-sugg" role="listbox">
            {items.map((s, i) => (
              <li key={s.kind + s.text} role="option" aria-selected={i === active}
                class={`filter-sugg-${s.kind}${i === active ? " is-active" : ""}`}
                onMouseDown={(e) => { e.preventDefault(); accept(s); }}
                onMouseEnter={() => setActive(i)}>
                <code>{s.text}</code>
                {s.hint && <span class="filter-sugg-hint">{s.hint}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
