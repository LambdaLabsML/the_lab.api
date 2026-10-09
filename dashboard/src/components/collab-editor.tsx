// Live collaborative Markdown editor (CodeMirror 6), driven by a CollabSession
// (see ../state/collab.ts for the OT protocol). Obsidian/Typora-style "live
// preview": Markdown markup is hidden except on the line the cursor/selection
// touches; code blocks and tables stay plain monospace text.
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { effect } from "@preact/signals";
import {
  EditorState, StateEffect, StateField, Annotation, Transaction,
  type Extension, type Range,
} from "@codemirror/state";
import {
  EditorView, Decoration, ViewPlugin, WidgetType, keymap, drawSelection, placeholder,
  type DecorationSet, type ViewUpdate,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { GFM } from "@lezer/markdown";
import { syntaxTree } from "@codemirror/language";
import { CollabSession } from "../state/collab";
import { opsFromChanges, changesFromOps } from "../lib/ot";

// ---------------------------------------------------------------------------
// Live-preview Markdown decorations
// ---------------------------------------------------------------------------

class BulletWidget extends WidgetType {
  eq(): boolean { return true; }
  toDOM(): HTMLElement {
    const span = document.createElement("span");
    span.className = "cm-md-bullet";
    span.textContent = "•";
    return span;
  }
}

class HrWidget extends WidgetType {
  eq(): boolean { return true; }
  toDOM(): HTMLElement {
    const div = document.createElement("div");
    div.className = "cm-md-hr";
    return div;
  }
}

function activeLines(state: EditorState): Set<number> {
  const lines = new Set<number>();
  for (const r of state.selection.ranges) {
    const a = state.doc.lineAt(r.from).number;
    const b = state.doc.lineAt(r.to).number;
    for (let l = a; l <= b; l++) lines.add(l);
  }
  return lines;
}

const HEADING_CLASS: Record<string, string> = {
  ATXHeading1: "cm-md-h1", ATXHeading2: "cm-md-h2", ATXHeading3: "cm-md-h3",
  ATXHeading4: "cm-md-h4", ATXHeading5: "cm-md-h5", ATXHeading6: "cm-md-h6",
};

/** Hide a marker range (and one trailing space, e.g. "# ", "> "). */
function hideThrough(doc: EditorState["doc"], from: number, to: number): [number, number] {
  let end = to;
  if (doc.sliceString(end, end + 1) === " ") end += 1;
  return [from, end];
}

function buildMarkdownDecorations(view: EditorView): DecorationSet {
  const ranges: Range<Decoration>[] = [];
  const state = view.state;
  const doc = state.doc;
  const active = activeLines(state);
  const tree = syntaxTree(state);

  for (const { from, to } of view.visibleRanges) {
    tree.iterate({
      from, to,
      enter(nodeRef) {
        const name = nodeRef.type.name;
        const lineActive = active.has(doc.lineAt(nodeRef.from).number);

        if (name in HEADING_CLASS) {
          if (!lineActive) {
            ranges.push(Decoration.mark({ class: HEADING_CLASS[name] }).range(nodeRef.from, nodeRef.to));
            const mark = nodeRef.node.firstChild;
            if (mark && mark.type.name === "HeaderMark") {
              const [a, b] = hideThrough(doc, mark.from, mark.to);
              ranges.push(Decoration.replace({}).range(a, b));
            }
          }
          return;
        }

        if (name === "Emphasis" || name === "StrongEmphasis" || name === "Strikethrough") {
          if (!lineActive) {
            const cls = name === "Emphasis" ? "cm-md-em" : name === "StrongEmphasis" ? "cm-md-strong" : "cm-md-strike";
            ranges.push(Decoration.mark({ class: cls }).range(nodeRef.from, nodeRef.to));
            const markName = name === "Strikethrough" ? "StrikethroughMark" : "EmphasisMark";
            for (const child of nodeRef.node.getChildren(markName)) {
              ranges.push(Decoration.replace({}).range(child.from, child.to));
            }
          }
          return;
        }

        if (name === "InlineCode") {
          if (!lineActive) {
            ranges.push(Decoration.mark({ class: "cm-md-code-inline" }).range(nodeRef.from, nodeRef.to));
            for (const child of nodeRef.node.getChildren("CodeMark")) {
              ranges.push(Decoration.replace({}).range(child.from, child.to));
            }
          }
          return;
        }

        if (name === "Link") {
          if (!lineActive) {
            ranges.push(Decoration.mark({ class: "cm-md-link" }).range(nodeRef.from, nodeRef.to));
            for (const child of nodeRef.node.getChildren("LinkMark")) {
              ranges.push(Decoration.replace({}).range(child.from, child.to));
            }
            for (const child of nodeRef.node.getChildren("URL")) {
              ranges.push(Decoration.replace({}).range(child.from, child.to));
            }
          }
          return;
        }

        if (name === "QuoteMark") {
          const line = doc.lineAt(nodeRef.from);
          ranges.push(Decoration.line({ class: "cm-md-quote-line" }).range(line.from, line.from));
          if (!lineActive) {
            const [a, b] = hideThrough(doc, nodeRef.from, nodeRef.to);
            ranges.push(Decoration.replace({}).range(a, b));
          }
          return;
        }

        if (name === "ListMark") {
          const parent = nodeRef.node.parent?.parent; // ListMark -> ListItem -> BulletList|OrderedList
          if (!lineActive && parent && parent.type.name === "BulletList") {
            ranges.push(Decoration.replace({ widget: new BulletWidget() }).range(nodeRef.from, nodeRef.to));
          }
          return;
        }

        if (name === "HorizontalRule") {
          if (!lineActive) {
            ranges.push(Decoration.replace({ widget: new HrWidget() }).range(nodeRef.from, nodeRef.to));
          }
          return;
        }

        if (name === "FencedCode") {
          const startLine = doc.lineAt(nodeRef.from).number;
          const endLine = doc.lineAt(Math.max(nodeRef.from, nodeRef.to - 1)).number;
          for (let l = startLine; l <= endLine; l++) {
            const ln = doc.line(l);
            ranges.push(Decoration.line({ class: "cm-md-codeblock" }).range(ln.from, ln.from));
          }
          return;
        }
      },
    });
  }

  return Decoration.set(ranges, true);
}

const markdownLivePreview = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) { this.decorations = buildMarkdownDecorations(view); }
    update(update: ViewUpdate): void {
      if (update.docChanged || update.selectionSet || update.viewportChanged) {
        this.decorations = buildMarkdownDecorations(update.view);
      }
    }
  },
  { decorations: (v) => v.decorations },
);

// ---------------------------------------------------------------------------
// Remote presence: carets + selection highlights, mapped through changes.
// ---------------------------------------------------------------------------

interface RemoteCursorState { id: string; name: string; color: string; anchor: number; head: number }

const setRemoteCursor = StateEffect.define<RemoteCursorState>();
const removeRemoteCursor = StateEffect.define<string>();

const remoteCursorsField = StateField.define<RemoteCursorState[]>({
  create() { return []; },
  update(value, tr) {
    // Positions are clamped: a cursor reported for text this editor doesn't
    // have yet (e.g. others' cursors in the "joined" frame, before the text
    // is loaded) would otherwise make mapPos throw and abort the transaction.
    const clampTo = (n: number, len: number) => Math.max(0, Math.min(n, len));
    let next = value;
    if (tr.docChanged) {
      const before = tr.startState.doc.length;
      next = next.map((c) => ({
        ...c,
        anchor: tr.changes.mapPos(clampTo(c.anchor, before)),
        head: tr.changes.mapPos(clampTo(c.head, before)),
      }));
    }
    for (const e of tr.effects) {
      if (e.is(setRemoteCursor)) {
        const len = tr.state.doc.length;
        next = next.filter((c) => c.id !== e.value.id)
          .concat({ ...e.value, anchor: clampTo(e.value.anchor, len), head: clampTo(e.value.head, len) });
      } else if (e.is(removeRemoteCursor)) {
        next = next.filter((c) => c.id !== e.value);
      }
    }
    return next;
  },
});

class CaretWidget extends WidgetType {
  constructor(private name: string, private color: string) { super(); }
  eq(other: CaretWidget): boolean { return other.name === this.name && other.color === this.color; }
  toDOM(): HTMLElement {
    const wrap = document.createElement("span");
    wrap.className = "cm-remote-caret";
    const bar = document.createElement("span");
    bar.className = "cm-remote-caret-bar";
    bar.style.background = this.color;
    const label = document.createElement("span");
    label.className = "cm-remote-caret-label";
    label.textContent = this.name;
    label.style.background = this.color;
    wrap.appendChild(bar);
    wrap.appendChild(label);
    return wrap;
  }
  ignoreEvent(): boolean { return true; }
}

function buildRemoteDecorations(state: EditorState): DecorationSet {
  const cursors = state.field(remoteCursorsField);
  const ranges: Range<Decoration>[] = [];
  const docLen = state.doc.length;
  for (const c of cursors) {
    const anchor = Math.max(0, Math.min(docLen, c.anchor));
    const head = Math.max(0, Math.min(docLen, c.head));
    if (anchor !== head) {
      const from = Math.min(anchor, head), to = Math.max(anchor, head);
      // ~25% opacity selection highlight — color is always "#rrggbb" (server palette).
      ranges.push(Decoration.mark({ attributes: { style: `background-color:${c.color}40` } }).range(from, to));
    }
    ranges.push(Decoration.widget({ widget: new CaretWidget(c.name, c.color), side: 1 }).range(head));
  }
  return Decoration.set(ranges, true);
}

const remoteCursorsPlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) { this.decorations = buildRemoteDecorations(view.state); }
    update(update: ViewUpdate): void {
      if (update.docChanged || update.state.field(remoteCursorsField) !== update.startState.field(remoteCursorsField)) {
        this.decorations = buildRemoteDecorations(update.state);
      }
    }
  },
  { decorations: (v) => v.decorations },
);

// ---------------------------------------------------------------------------
// Theme — reads the dashboard's CSS variables so it follows the active theme.
// ---------------------------------------------------------------------------

const collabTheme = EditorView.theme({
  "&": { backgroundColor: "var(--bg-elev)", color: "var(--text)", height: "100%" },
  "&.cm-focused": { outline: "none" },
  ".cm-content": {
    fontFamily: "var(--font-mono)", fontSize: "var(--text-sm)", lineHeight: "1.6",
    caretColor: "var(--text)", padding: "10px 12px",
  },
  ".cm-scroller": { overflow: "auto" },
  ".cm-gutters": { display: "none" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
    backgroundColor: "color-mix(in srgb, var(--accent) 25%, transparent) !important",
  },
  ".cm-line": { padding: "0 2px" },
});

/** Annotation tagging a transaction as applying a REMOTE op (not the local
 *  user's own edit) — kept out of submitLocalOp and out of local undo history. */
const remoteOrigin = Annotation.define<boolean>();

// ---------------------------------------------------------------------------
// CollabEditor component
// ---------------------------------------------------------------------------

function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  const clean = name.replace(/[^a-zA-Z0-9]/g, "");
  return (clean || name || "?").slice(0, 2).toUpperCase();
}

export function CollabEditor({ path, onClose }: { path: string; onClose?: () => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const [session] = useState(() => new CollabSession(path));
  const [ready, setReady] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);

  useEffect(() => {
    const parent = containerRef.current;
    if (!parent) return;

    const extensions: Extension[] = [
      history(),
      keymap.of([
        { key: "Mod-s", run: () => { setFlash("saved automatically"); setTimeout(() => setFlash(null), 1500); return true; } },
        indentWithTab,
        ...defaultKeymap,
        ...historyKeymap,
      ]),
      markdown({ extensions: GFM }), // GFM: strikethrough, tables, task lists, autolinks
      markdownLivePreview,
      remoteCursorsField,
      remoteCursorsPlugin,
      drawSelection(),
      EditorView.lineWrapping,
      collabTheme,
      placeholder("Start typing…"),
      EditorView.updateListener.of((update: ViewUpdate) => {
        const isRemote = update.transactions.some((tr) => tr.annotation(remoteOrigin));
        if (isRemote) return; // remote-applied change: already reflected in session state
        for (const tr of update.transactions) {
          if (!tr.docChanged) continue;
          const op = opsFromChanges(tr.changes, tr.startState.doc.toString());
          session.submitLocalOp(op, tr.state.doc.toString());
        }
        if (update.docChanged || update.selectionSet) {
          const sel = update.state.selection.main;
          session.sendCursor(sel.anchor, sel.head);
        }
      }),
    ];

    const view = new EditorView({ state: EditorState.create({ doc: "", extensions }), parent });
    viewRef.current = view;
    // Reachable from devtools / browser tests: document.querySelector(".cm-editor").__labView
    (view.dom as any).__labView = view;

    session.onResync = (text: string) => {
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: text },
        annotations: [remoteOrigin.of(true), Transaction.addToHistory.of(false)],
      });
      setReady(true);
    };
    session.onRemoteChange = (changes) => {
      if (!changes.length) return;
      view.dispatch({
        changes,
        annotations: [remoteOrigin.of(true), Transaction.addToHistory.of(false)],
      });
    };
    // The socket opens with the session (before this effect): if "joined" and
    // early ops already arrived, start from the session's text.
    if (session.hasJoined) session.onResync?.(session.currentText);

    // Project session.participants (incl. remote cursor positions) onto the
    // view's remote-cursor field whenever it changes.
    const disposeParticipants = effect(() => {
      const list = session.participants.value;
      const v = viewRef.current;
      if (!v) return;
      const effects: StateEffect<unknown>[] = [];
      const seen = new Set<string>();
      for (const p of list) {
        if (p.id === session.selfId) continue;
        seen.add(p.id);
        if (p.anchor == null || p.head == null) effects.push(removeRemoteCursor.of(p.id));
        else effects.push(setRemoteCursor.of({ id: p.id, name: p.name, color: p.color, anchor: p.anchor, head: p.head }));
      }
      for (const c of v.state.field(remoteCursorsField)) {
        if (!seen.has(c.id)) effects.push(removeRemoteCursor.of(c.id));
      }
      if (effects.length) v.dispatch({ effects });
    });

    return () => {
      disposeParticipants();
      session.onResync = null;
      session.onRemoteChange = null;
      view.destroy();
      viewRef.current = null;
      session.dispose();
    };
  }, [session]);

  const status = session.status.value;
  const lastSaved = session.lastSavedAt.value;
  const fatalError = session.fatalError.value;
  const participants = session.participants.value;
  const ordered = useMemo(() => {
    const self = participants.find((p) => p.id === session.selfId);
    const others = participants.filter((p) => p.id !== session.selfId);
    return self ? [self, ...others] : others;
  }, [participants, session]);

  const statusText = flash ? flash
    : fatalError ? fatalError
    : status === "connecting" ? "connecting…"
    : status === "reconnecting" ? "reconnecting…"
    : status === "disconnected" ? "disconnected"
    : lastSaved ? `saved ${new Date(lastSaved).toLocaleTimeString([], { hour12: false })}`
    : "connected";

  return (
    <div class="collab-editor">
      <div class="collab-bar">
        <div class="collab-avatars">
          {ordered.map((p) => (
            <span
              key={p.id}
              class="collab-avatar"
              style={{ background: p.id === session.selfId ? session.selfColor.value : p.color }}
              title={p.id === session.selfId ? `${p.name} (you)` : p.name}
            >
              {initialsOf(p.name)}
            </span>
          ))}
        </div>
        <span class={`collab-status is-${flash ? "ok" : status}`}>{statusText}</span>
        <span class="collab-bar-spacer" />
        <button type="button" class="art-btn" onClick={() => session.invite()} title="Ping everyone else on the dashboard to come here">
          Invite others
        </button>
        {onClose && <button type="button" class="art-btn" onClick={onClose}>Close</button>}
      </div>
      <div class="collab-cm-host" ref={containerRef} data-ready={ready ? "true" : "false"} />
      {!ready && <div class="art-note">Connecting…</div>}
    </div>
  );
}
