import { useEffect, useLayoutEffect, useState, useRef } from "preact/hooks";
import { selectedIdea, selectedMetric, detailTimeline, detailSortNewest } from "../state/settings";
import { scrollToExperiment, runningProgress, allExperiments } from "../state/signals";
import { getIdea, getExperimentProgress, getExperimentLog, getExperimentScript, getExperimentOutput, getIdeaDiff } from "../state/api";
import { formatTime, badgeHtml, escapeHtml } from "../lib/format";
import { Lightbox } from "./lightbox";
import { JsonView } from "./json-view";
import { Eyebrow, Stat, Badge, Toggle, EmptyState, type BadgeTone } from "./ui";
import { useEntityNav, useDisclosure } from "../lib/hooks";
import type { IdeaDetail, Experiment, Note } from "../lib/types";
import { getStatusColor, isLowerBetter } from "../lib/colors";

// Map experiment status / note level → a <Badge> tone.
const STATUS_TONE: Record<string, BadgeTone> = {
  running: "running",
  pending: "neutral",
  completed: "good",
  failed: "bad",
  cancelled: "neutral",
};
const LEVEL_TONE: Record<string, BadgeTone> = {
  milestone: "warn",
  insight: "concluded",
  observation: "neutral",
  debug: "bad",
  note: "neutral",
};

// ---------------------------------------------------------------------------
// URL hash helpers — encode/decode lightbox state as shareable deep links.
// Format: #idea=5&exp=exp001&view=log  (view: log | script | output | diff)
// ---------------------------------------------------------------------------

function parseHash(): Record<string, string> {
  const h = window.location.hash.slice(1);
  if (!h) return {};
  try { return Object.fromEntries(new URLSearchParams(h)); } catch { return {}; }
}

function setHash(params: Record<string, string | number | null>) {
  const next = parseHash();
  for (const [k, v] of Object.entries(params)) {
    if (v === null) delete next[k];
    else next[k] = String(v);
  }
  const qs = new URLSearchParams(next).toString();
  history.replaceState(null, "", qs ? `#${qs}` : location.pathname + location.search);
}

/**
 * @param embedded  When true, the panel is rendered inside a host that already
 *   provides an "Idea Detail" heading + idea identity (the Overview review's
 *   `.review-detail-flush` section, which renders its own `<h2>Idea Detail</h2>`
 *   and an `idea #N · best …` sub-line). In that context we suppress the panel's
 *   own top-level identity chrome (`.detail-head`: the "Idea #N" title, status
 *   badge, and close button) so the header isn't shown twice. Defaults to false
 *   (the standalone workbench dockview panel keeps its full identity header).
 */
export function DetailPanel({ embedded = false }: { embedded?: boolean } = {}) {
  const ideaId = selectedIdea.value;
  const [idea, setIdea] = useState<IdeaDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const pollRef = useRef<number | null>(null);
  const fetchRef = useRef(0);
  const hashHandledForIdea = useRef<number | null>(null);

  // Log lightbox
  const [logExp, setLogExp] = useState<Experiment | null>(null);
  const [logContent, setLogContent] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const [logFollowing, setLogFollowing] = useState(true);
  const logBodyRef = useRef<HTMLDivElement>(null);
  const logPollRef = useRef<number | null>(null);

  // Script lightbox
  const [scriptExp, setScriptExp] = useState<Experiment | null>(null);
  const [scriptContent, setScriptContent] = useState<string | null>(null);
  const [scriptLoading, setScriptLoading] = useState(false);

  // Output lightbox
  const [outputExp, setOutputExp] = useState<Experiment | null>(null);
  const [outputContent, setOutputContent] = useState<string | null>(null);
  const [outputBasePath, setOutputBasePath] = useState("");
  const [outputFormat, setOutputFormat] = useState<"md" | "html">("md");
  const [outputLoading, setOutputLoading] = useState(false);
  const [outputFollowing, setOutputFollowing] = useState(true);
  const outputBodyRef = useRef<HTMLDivElement>(null);
  const outputPollRef = useRef<number | null>(null);
  // Remember open/closed state of <details> across polls so collapsible
  // sections survive the 5s refresh. Keyed by summary text; falls back to
  // position index when two summaries are identical.
  const outputDetailsRef = useRef<Map<string, boolean>>(new Map());
  // Navigation stack for clicking local .md / .html links inside the output
  // viewer. When non-empty, the top entry's content/basePath/format replaces
  // the exp output.
  // - parentScroll: scrollTop of the lightbox body before navigating into
  //   this entry; restored when popped.
  // - anchor: id of an element to scroll to on first render of this entry
  //   (set when the link's URL had a #fragment).
  // - format: how to render the content ("md" runs the markdown renderer,
  //   "html" injects directly).
  const [outputFileStack, setOutputFileStack] = useState<
    Array<{ path: string; content: string; basePath: string; format: "md" | "html"; parentScroll: number; anchor?: string }>
  >([]);
  const [outputFileLoading, setOutputFileLoading] = useState(false);
  // One-shot scroll intent applied by the post-render layout effect.
  const pendingScrollRef = useRef<{ kind: "anchor"; id: string } | { kind: "top"; value: number } | null>(null);

  // Diff lightbox
  const [diffOpen, setDiffOpen] = useState(false);
  const [diffUseMain, setDiffUseMain] = useState(false);
  const [diffData, setDiffData] = useState<{ stat: string; diff: string } | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);

  // Progress data per experiment (keyed by exp id)
  const [progressData, setProgressData] = useState<Record<string, Record<string, any>>>({});

  // On mount: read hash and navigate to the encoded idea
  useEffect(() => {
    const h = parseHash();
    if (h.idea) selectedIdea.value = Number(h.idea);
  }, []);

  // Once an idea loads, auto-open the lightbox encoded in the hash (first time only per idea)
  useEffect(() => {
    if (!idea || hashHandledForIdea.current === idea.id) return;
    hashHandledForIdea.current = idea.id;
    const h = parseHash();
    if (!h.view || Number(h.idea) !== idea.id) return;
    if (h.view === "diff") { openDiff(false); return; }
    if (!h.exp) return;
    const exp = idea.experiments?.find(e => (e.label || String(e.id)) === h.exp);
    if (!exp) return;
    if (h.view === "log") openLog(exp);
    else if (h.view === "script") openScript(exp);
    else if (h.view === "output") openOutput(exp);
  }, [idea]);

  // Fetch idea data on selection change + poll every 10s for updates
  useEffect(() => {
    if (ideaId === null) {
      setIdea(null);
      setLoading(false);
      return;
    }
    const seq = ++fetchRef.current;
    setLoading(true);

    function fetchData() {
      getIdea(ideaId, true)
        .then((data) => { if (fetchRef.current === seq) { setIdea(data); setLoading(false); } })
        .catch(() => { if (fetchRef.current === seq) { setIdea(null); setLoading(false); } });
    }

    fetchData();
    const timer = setInterval(fetchData, 10_000);
    return () => clearInterval(timer);
  }, [ideaId]);

  // Scroll-to-pane THEN highlight (#50). When `scrollToExperiment` fires we
  // scroll the matching experiment card into view, wait for the scroll to
  // SETTLE, and only then briefly flash ONLY that one card.
  //
  // Context: the panel is embedded in the Overview/Review page inside a
  // scrolling ancestor (.app-scroll), so scrollIntoView({block:"center"})
  // walks every scroll-ancestor. The target card may not be in the DOM yet
  // (navigation switches idea, then getIdea() resolves and the cards render),
  // so we retry across animation frames until it exists. We match the exact
  // card by the label the signal carries — `exp.label || exp.id`, the same
  // shape chartNavClick/useEntityNav produce — with CSS.escape to guard labels
  // with special chars. The transient .exp-flash class drives a self-fading
  // keyframe; remove → reflow → add replays it on a repeat click.
  //
  // The whole sequence is tracked in a ref (not the effect closure) so that
  // clearing the signal — which re-renders and re-runs this effect — does NOT
  // cancel the in-flight scroll-settle/flash. We only abort a run when a NEW
  // run supersedes it or the component unmounts.
  const flashOpRef = useRef<{ cancel: () => void } | null>(null);
  useEffect(() => {
    const label = scrollToExperiment.value;
    if (!label) return;

    // Supersede any in-flight run, then start a fresh one.
    flashOpRef.current?.cancel();

    let cancelled = false;
    let rafId = 0;
    const timeouts: number[] = [];
    let onEnd: (() => void) | null = null;
    const sel = (window as any).CSS?.escape ? CSS.escape(label) : label;
    const op = {
      cancel() {
        cancelled = true;
        if (rafId) window.cancelAnimationFrame(rafId);
        timeouts.forEach((t) => window.clearTimeout(t));
        if (onEnd) window.removeEventListener("scrollend", onEnd, true);
      },
    };
    flashOpRef.current = op;

    function flash(el: HTMLElement) {
      el.classList.remove("exp-flash");
      // Force reflow so removing + re-adding restarts the keyframe (replay).
      void el.offsetWidth;
      el.classList.add("exp-flash");
      timeouts.push(window.setTimeout(() => el.classList.remove("exp-flash"), 1000));
    }

    function afterScroll(el: HTMLElement) {
      // Highlight only once the scroll has come to rest. Prefer the native
      // `scrollend` event (fires on the scroll-ancestor and bubbles to window
      // in capture); fall back to a ~500ms timeout for browsers/containers that
      // don't emit it (and as a hard cap when the card was already in view and
      // no scroll occurred). Then add a small extra beat (~200ms) before the
      // flash so it reads as "arrive, then highlight" (#62) rather than firing
      // the instant the scroll ends.
      let done = false;
      const fire = () => {
        if (done || cancelled) return;
        done = true;
        if (onEnd) window.removeEventListener("scrollend", onEnd, true);
        // Keep `op` referenced through the extra beat so a superseding click
        // can still cancel this pending flash; release once it actually fires.
        timeouts.push(window.setTimeout(() => {
          if (cancelled) return;
          if (flashOpRef.current === op) flashOpRef.current = null;
          flash(el);
        }, 200));
      };
      onEnd = fire;
      window.addEventListener("scrollend", onEnd, true);
      timeouts.push(window.setTimeout(fire, 500));
    }

    // Retry-find the card: it may render a frame or two after the idea loads.
    let tries = 0;
    function attempt() {
      if (cancelled) return;
      const el = document.querySelector(`.exp-item[data-exp-label="${sel}"]`) as HTMLElement | null;
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        afterScroll(el);
        // Mark handled — clearing the signal re-runs this effect; the ref keeps
        // the pending scroll-settle/flash alive across that re-render.
        scrollToExperiment.value = null;
        return;
      }
      if (++tries > 30) {            // ~0.5s of frames; card never appeared
        if (flashOpRef.current === op) flashOpRef.current = null;
        scrollToExperiment.value = null;
        return;
      }
      rafId = window.requestAnimationFrame(attempt);
    }
    attempt();

    // NOTE: no teardown here — we deliberately do not cancel on the re-render
    // caused by clearing the signal. Cancellation happens via flashOpRef when a
    // newer run starts; the unmount-cleanup effect below aborts the last run.
  }, [scrollToExperiment.value, idea]);

  // Abort any in-flight flash op when the panel unmounts.
  useEffect(() => () => flashOpRef.current?.cancel(), []);

  // Progress polling
  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    if (!idea) return;
    const running = (idea.experiments || []).filter((e) => e.status === "running");
    if (running.length === 0) return;
    function pollProgress() {
      running.forEach((exp) => {
        const label = exp.label || String(exp.id);
        getExperimentProgress(label).then((data) => {
          if (data.progress) {
            setProgressData((prev) => ({ ...prev, [String(exp.id)]: data.progress as Record<string, any> }));
            const pct = (data.progress as any).pct_complete ?? (data.progress as any).pct;
            if (typeof pct === "number") {
              runningProgress.value = { ...runningProgress.value, [label]: pct };
            }
          }
        });
      });
    }
    pollProgress();
    pollRef.current = window.setInterval(pollProgress, 3000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [idea]);

  // --- Log ---

  function fetchLogContent(exp: Experiment) {
    getExperimentLog(exp.label || exp.id)
      .then((data) => { setLogContent(data.log); setLogLoading(false); })
      .catch(() => { setLogContent("Failed to load log."); setLogLoading(false); });
  }

  // Auto-refresh log when running
  useEffect(() => {
    if (logPollRef.current) { clearInterval(logPollRef.current); logPollRef.current = null; }
    if (!logExp || logExp.status !== "running") return;
    logPollRef.current = window.setInterval(() => fetchLogContent(logExp), 3000);
    return () => { if (logPollRef.current) { clearInterval(logPollRef.current); logPollRef.current = null; } };
  }, [logExp]);

  // Auto-scroll log when following and content updates
  useEffect(() => {
    if (logFollowing && logBodyRef.current) {
      logBodyRef.current.scrollTop = logBodyRef.current.scrollHeight;
    }
  }, [logContent]);

  function openLog(exp: Experiment) {
    setLogExp(exp);
    setLogContent(null);
    setLogLoading(true);
    setLogFollowing(true);
    fetchLogContent(exp);
    setHash({ idea: ideaId, exp: exp.label || exp.id, view: "log" });
  }

  // --- Output ---

  function fetchOutputContent(exp: Experiment) {
    getExperimentOutput(exp.label || exp.id)
      .then((data) => {
        setOutputContent(data.output);
        setOutputBasePath(data.base_path || "");
        setOutputFormat((data.format as "md" | "html") || "md");
        setOutputLoading(false);
      })
      .catch(() => { setOutputContent("(output file not found)"); setOutputLoading(false); });
  }

  // Auto-refresh output when running
  useEffect(() => {
    if (outputPollRef.current) { clearInterval(outputPollRef.current); outputPollRef.current = null; }
    if (!outputExp || outputExp.status !== "running") return;
    outputPollRef.current = window.setInterval(() => fetchOutputContent(outputExp), 5000);
    return () => { if (outputPollRef.current) { clearInterval(outputPollRef.current); outputPollRef.current = null; } };
  }, [outputExp]);

  // Currently-displayed file: top of stack if any linked .md is open, else
  // the experiment's own output. Effects below depend on these so they
  // re-run when the user navigates linked files or the poll fetches new
  // content.
  const _outputLinkedTop = outputFileStack.length > 0 ? outputFileStack[outputFileStack.length - 1] : null;
  const displayedContent = _outputLinkedTop ? _outputLinkedTop.content : outputContent;
  const displayedBasePath = _outputLinkedTop ? _outputLinkedTop.basePath : outputBasePath;
  const displayedFormat: "md" | "html" = _outputLinkedTop ? _outputLinkedTop.format : outputFormat;

  // Track which inline scripts we've already executed in this context so
  // poll refreshes don't re-run them and stack intervals/listeners.
  const executedScriptsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    executedScriptsRef.current = new Set();
  }, [outputExp?.id, outputFileStack.length]);

  // Auto-scroll output when following and content updates
  useEffect(() => {
    if (outputFollowing && outputBodyRef.current) {
      outputBodyRef.current.scrollTop = outputBodyRef.current.scrollHeight;
    }
  }, [outputContent]);

  // Restore open <details> sections after each poll. Runs synchronously
  // before the browser paints so readers never see a flash of "closed".
  // Also: rewrite raw-HTML relative URLs (src/href) to /api/v1/files/...
  // and re-execute inline <script> tags (innerHTML never runs them).
  useLayoutEffect(() => {
    const root = outputBodyRef.current;
    if (!root || displayedContent === null) return;

    // 1. Restore <details> open state
    const list = root.querySelectorAll<HTMLDetailsElement>("details");
    const seen = new Map<string, number>();
    list.forEach((d, i) => {
      const summary = d.querySelector("summary");
      const base = summary?.textContent?.trim() || `idx-${i}`;
      const dup = seen.get(base) || 0;
      seen.set(base, dup + 1);
      const key = dup === 0 ? base : `${base}#${dup}`;
      const saved = outputDetailsRef.current.get(key);
      if (saved !== undefined) d.open = saved;
    });

    // 2. Rewrite relative src/href in raw HTML so e.g. <img src="grids/x.png">
    //    resolves to /api/v1/files/<basePath>/grids/x.png. Markdown-generated
    //    links/images already have absolute paths via resolveUrl, so the
    //    starts-with-slash check leaves them alone.
    const isAbs = (u: string) => /^(https?:|data:|blob:|\/|#|mailto:)/i.test(u);
    const prefix = `/api/v1/files/${displayedBasePath}/`;
    root.querySelectorAll<HTMLImageElement>("img[src]").forEach((el) => {
      const v = el.getAttribute("src") || "";
      if (!isAbs(v)) el.setAttribute("src", prefix + v);
    });
    root.querySelectorAll<HTMLAnchorElement>("a[href]").forEach((el) => {
      const v = el.getAttribute("href") || "";
      if (!isAbs(v)) el.setAttribute("href", prefix + v);
    });
    root.querySelectorAll<HTMLElement>(
      "source[src], iframe[src], video[src], audio[src], script[src]"
    ).forEach((el) => {
      const v = el.getAttribute("src") || "";
      if (!isAbs(v)) el.setAttribute("src", prefix + v);
    });

    // 3. Re-execute <script> tags. innerHTML inserts them but the browser
    //    won't run them — we have to detach + create + insert. Dedupe by
    //    code text so polling refreshes on a running experiment don't stack
    //    intervals/listeners; reset of the dedupe set happens when the
    //    user switches experiment or navigates a linked file.
    root.querySelectorAll<HTMLScriptElement>("script").forEach((old) => {
      const code = (old.textContent || "") + "" + (old.getAttribute("src") || "");
      if (executedScriptsRef.current.has(code)) {
        old.remove();
        return;
      }
      executedScriptsRef.current.add(code);
      const fresh = document.createElement("script");
      for (const attr of Array.from(old.attributes)) {
        try { fresh.setAttribute(attr.name, attr.value); } catch { /* ignore */ }
      }
      if (old.textContent) fresh.textContent = old.textContent;
      old.replaceWith(fresh);
    });

    // 4. Render mermaid diagrams. Lazy-loads the library on first encounter
    //    (~600 KB). Mermaid v10 stamps data-processed="true" on rendered
    //    elements and skips them on later run() calls, so re-renders after
    //    polling are cheap.
    const mermaidNodes = Array.from(
      root.querySelectorAll<HTMLElement>(".mermaid:not([data-processed])"),
    );
    if (mermaidNodes.length > 0) {
      ensureMermaid()
        .then((mermaid) => mermaid.run({
          nodes: mermaidNodes,
          suppressErrors: true,
        }))
        .catch((e) => {
          console.warn("mermaid render failed:", e);
          mermaidNodes.forEach((n) => {
            n.setAttribute("data-processed", "true");
            n.style.color = "var(--red)";
            n.textContent = `(mermaid render failed: ${e?.message || e})\n` + (n.textContent || "");
          });
        });
    }

    // 5. Apply any pending scroll intent (anchor link, restore-on-back).
    //    Single-shot — clear the ref so re-renders for the same content
    //    (e.g. <details> open/close) don't re-jump the user.
    const intent = pendingScrollRef.current;
    if (intent) {
      pendingScrollRef.current = null;
      if (intent.kind === "anchor") {
        try {
          const sel = (window as any).CSS?.escape ? CSS.escape(intent.id) : intent.id;
          const target = root.querySelector(`#${sel}`) as HTMLElement | null;
          if (target) target.scrollIntoView({ behavior: "auto", block: "start" });
          else root.scrollTop = 0;
        } catch {
          root.scrollTop = 0;
        }
      } else {
        root.scrollTop = intent.value;
      }
    }
  }, [displayedContent, displayedBasePath]);

  // Listen for <details> toggle events inside the output body. `toggle`
  // doesn't bubble, so we use capture-phase delegation on the container.
  useEffect(() => {
    const root = outputBodyRef.current;
    if (!root || !outputExp) return;
    const handler = (e: Event) => {
      const target = e.target as HTMLElement;
      if (target.tagName !== "DETAILS") return;
      const d = target as HTMLDetailsElement;
      const all = Array.from(root.querySelectorAll<HTMLDetailsElement>("details"));
      const idx = all.indexOf(d);
      if (idx < 0) return;
      const seen = new Map<string, number>();
      let key = `idx-${idx}`;
      for (let i = 0; i <= idx; i++) {
        const summary = all[i].querySelector("summary");
        const base = summary?.textContent?.trim() || `idx-${i}`;
        const dup = seen.get(base) || 0;
        seen.set(base, dup + 1);
        if (i === idx) key = dup === 0 ? base : `${base}#${dup}`;
      }
      outputDetailsRef.current.set(key, d.open);
    };
    root.addEventListener("toggle", handler, true);
    return () => root.removeEventListener("toggle", handler, true);
  }, [outputExp]);

  // Intercept clicks on local .md links inside the output viewer so they
  // render in-place rather than opening the raw file in a new tab.
  // Also handles in-page #anchor links and cross-file file.md#anchor links.
  useEffect(() => {
    const root = outputBodyRef.current;
    if (!root || !outputExp) return;
    const handler = (e: MouseEvent) => {
      const anchor = (e.target as HTMLElement | null)?.closest?.("a") as HTMLAnchorElement | null;
      if (!anchor) return;
      const hrefAttr = anchor.getAttribute("href") || "";
      if (!hrefAttr) return;

      // Pure same-page anchor (#section) — scroll to the matching id WITHOUT
      // navigating or pushing onto the stack. Avoids a hash change that
      // would interfere with the dashboard's deep-link logic.
      if (hrefAttr.startsWith("#")) {
        const id = hrefAttr.slice(1);
        if (!id) return;
        e.preventDefault();
        try {
          const target = root.querySelector(`#${(window as any).CSS?.escape ? CSS.escape(id) : id}`);
          if (target) (target as HTMLElement).scrollIntoView({ behavior: "smooth", block: "start" });
        } catch { /* invalid selector */ }
        return;
      }

      // Resolve to an absolute URL so we can compare origin + pathname.
      let resolved: URL;
      try {
        resolved = new URL(hrefAttr, window.location.href);
      } catch { return; }
      if (resolved.origin !== window.location.origin) return;
      if (!resolved.pathname.startsWith("/api/v1/files/")) return;
      if (!/\.(md|html?)$/i.test(resolved.pathname)) return;

      e.preventDefault();
      const filePath = decodeURIComponent(resolved.pathname.slice("/api/v1/files/".length));
      const slashIdx = filePath.lastIndexOf("/");
      const newBasePath = slashIdx >= 0 ? filePath.slice(0, slashIdx) : "";
      const linkFormat: "md" | "html" = /\.html?$/i.test(filePath) ? "html" : "md";
      const linkAnchor = resolved.hash ? resolved.hash.slice(1) : undefined;
      const parentScroll = root.scrollTop;

      setOutputFileLoading(true);
      // Reset details memory — linked file has different summaries.
      outputDetailsRef.current = new Map();
      // Drop hash from the fetch URL so the server doesn't see it in the path.
      const fetchUrl = `${resolved.origin}${resolved.pathname}`;
      fetch(fetchUrl, { cache: "no-cache" })
        .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`${r.status}`))))
        .then((content) => {
          // Tell the post-render layout effect where to scroll once the new
          // content is in the DOM.
          pendingScrollRef.current = linkAnchor
            ? { kind: "anchor", id: linkAnchor }
            : { kind: "top", value: 0 };
          setOutputFileStack((stack) => [...stack, {
            path: filePath, content, basePath: newBasePath, format: linkFormat, parentScroll, anchor: linkAnchor,
          }]);
          setOutputFollowing(false);
        })
        .catch(() => {
          pendingScrollRef.current = { kind: "top", value: 0 };
          setOutputFileStack((stack) => [...stack, {
            path: filePath,
            content: `(failed to load ${filePath})`,
            basePath: newBasePath,
            format: linkFormat,
            parentScroll,
          }]);
        })
        .finally(() => setOutputFileLoading(false));
    };
    root.addEventListener("click", handler);
    return () => root.removeEventListener("click", handler);
  }, [outputExp]);

  function openOutput(exp: Experiment) {
    // Switching experiments clears the persisted state — different
    // output.md files shouldn't share open/closed memory.
    if (outputExp?.id !== exp.id) outputDetailsRef.current = new Map();
    setOutputExp(exp);
    setOutputContent(null);
    setOutputLoading(true);
    setOutputFollowing(true);
    setOutputFileStack([]);  // drop any previous linked-file navigation
    fetchOutputContent(exp);
    setHash({ idea: ideaId, exp: exp.label || exp.id, view: "output" });
  }

  // --- Script ---

  function openScript(exp: Experiment) {
    setScriptExp(exp);
    setScriptContent(null);
    setScriptLoading(true);
    getExperimentScript(exp.label || exp.id)
      .then((data) => setScriptContent(data.script))
      .catch(() => setScriptContent("Failed to load script"))
      .finally(() => setScriptLoading(false));
    setHash({ idea: ideaId, exp: exp.label || exp.id, view: "script" });
  }

  // --- Diff ---

  function openDiff(useMain = false) {
    setDiffOpen(true);
    setDiffUseMain(useMain);
    loadDiff(useMain);
    setHash({ idea: ideaId, view: "diff", exp: null });
  }

  function loadDiff(useMain: boolean) {
    if (!idea) return;
    setDiffLoading(true);
    setDiffData(null);
    getIdeaDiff(idea.id, useMain ? "main" : undefined)
      .then(setDiffData)
      .catch(() => setDiffData(null))
      .finally(() => setDiffLoading(false));
  }

  function toggleDiffBase() {
    const next = !diffUseMain;
    setDiffUseMain(next);
    loadDiff(next);
  }

  if (ideaId === null) {
    return (
      <div id="detail-panel" class="open">
        <div id="detail-content">
          <EmptyState
            icon="⬡"
            title="No idea selected"
            body="Click any node in the Graph, a row in the Table, a dot in the chart, or a bar in the Timeline to load idea details here."
          />
        </div>
      </div>
    );
  }

  function close() {
    selectedIdea.value = null;
  }

  const notes: Note[] = idea?.notes || [];
  const experiments: Experiment[] = idea?.experiments || [];

  // --- Sorting ---
  const newestFirst = detailSortNewest.value;
  const sortMult = newestFirst ? -1 : 1;
  function byTime<T>(getTs: (x: T) => string | undefined) {
    return (a: T, b: T) => sortMult * (new Date(getTs(a) || 0).getTime() - new Date(getTs(b) || 0).getTime());
  }
  const sortedNotes = [...notes].sort(byTime<Note>(n => n.created_at));
  const sortedExps  = [...experiments].sort(byTime<Experiment>(e => e.created_at));

  // --- Timeline items ---
  type TLItem = { kind: "note"; note: Note; t: number } | { kind: "exp"; exp: Experiment; t: number };
  const tlItems: TLItem[] = detailTimeline.value ? [
    ...notes.map(n => ({ kind: "note" as const, note: n, t: new Date(n.created_at || 0).getTime() })),
    ...experiments.map(e => ({ kind: "exp" as const, exp: e, t: new Date(e.created_at || 0).getTime() })),
  ].sort((a, b) => sortMult * (a.t - b.t)) : [];

  // --- Stats for meta block ---
  const expByStatus = experiments.reduce((acc, e) => { acc[e.status] = (acc[e.status] || 0) + 1; return acc; }, {} as Record<string, number>);
  const noteByLevel = notes.reduce((acc, n) => { const l = n.level || "note"; acc[l] = (acc[l] || 0) + 1; return acc; }, {} as Record<string, number>);

  const STATUS_ORDER = ["running", "pending", "completed", "failed", "cancelled"];
  const LEVEL_ORDER  = ["milestone", "insight", "observation", "debug"];

  return (
    <>
      <div id="detail-panel" class="open">
        {loading && <div class="detail-loading-bar" />}
        <div id="detail-content">
          {/* Identity header — suppressed when embedded, since the review section
              already renders an "Idea Detail" heading + "idea #N" sub-line (#85). */}
          {!embedded && (
            <div class="detail-head">
              <span class="detail-head-title">
                <span class="detail-head-id">Idea #{ideaId}</span>
                {idea && <span dangerouslySetInnerHTML={{ __html: badgeHtml(idea.status) }} />}
              </span>
              <button type="button" class="detail-close" onClick={close} title="Close">&times;</button>
            </div>
          )}

          {loading && !idea && (
            <div class="detail-loading-text">Loading…</div>
          )}

          {idea && (
            <>
              {/* Compact meta block — shown FIRST so key stats are above the fold.
                  Flat --bg surface, hairline-separated rows; no boxed border. */}
              <div class="idea-meta-block">

                {/* When embedded the identity header is suppressed; surface the
                    status badge here so the idea's status stays visible (it is
                    NOT shown by the review heading). */}
                {embedded && (
                  <div class="meta-row">
                    <span dangerouslySetInnerHTML={{ __html: badgeHtml(idea.status) }} />
                  </div>
                )}

                {/* Row 1: branch + parents + diff */}
                {(idea.branch || (idea.parent_ids && idea.parent_ids.length > 0)) && (
                  <div class="meta-row">
                    {idea.branch && (
                      <>
                        <span class="meta-branch" title={idea.branch}>⎇ {idea.branch}</span>
                        <Toggle active={false} onClick={() => openDiff(false)} title="View branch diff">diff ↗</Toggle>
                      </>
                    )}
                    {idea.parent_ids && idea.parent_ids.length > 0 && (
                      <span class="meta-parents">
                        <span class="meta-arrow">←</span>
                        {idea.parent_ids.map((pid) => (
                          <ParentLink key={pid} pid={pid} />
                        ))}
                      </span>
                    )}
                  </div>
                )}

                {/* Row 2: experiment stats */}
                {experiments.length > 0 && (
                  <div class="meta-row meta-stats-row">
                    <Eyebrow class="meta-stat-label">{experiments.length} exp</Eyebrow>
                    {STATUS_ORDER.filter(s => expByStatus[s]).map(s => (
                      <Badge key={s} tone={STATUS_TONE[s] ?? "neutral"}>{expByStatus[s]} {s}</Badge>
                    ))}
                  </div>
                )}

                {/* Row 3: note stats */}
                {notes.length > 0 && (
                  <div class="meta-row meta-stats-row">
                    <Eyebrow class="meta-stat-label">{notes.length} notes</Eyebrow>
                    {LEVEL_ORDER.filter(l => noteByLevel[l]).map(l => (
                      <Badge key={l} tone={LEVEL_TONE[l] ?? "neutral"}>{noteByLevel[l]} {l}</Badge>
                    ))}
                    {noteByLevel["note"] ? <Badge tone="neutral">{noteByLevel["note"]} note</Badge> : null}
                  </div>
                )}

                {/* Best metric score for this idea — reads like a <Stat>. */}
                {(() => {
                  const mk = selectedMetric.value;
                  if (!mk || experiments.length === 0) return null;
                  const lower = isLowerBetter(mk);
                  let best: number | null = null;
                  for (const e of experiments) {
                    if (typeof e.metrics?.[mk] !== "number" || e._running) continue;
                    const v = e.metrics![mk] as number;
                    if (best === null || (lower ? v < best : v > best)) best = v;
                  }
                  if (best === null) return null;
                  const fmtBest = Math.abs(best) >= 100 ? best.toFixed(0) : Math.abs(best) >= 1 ? best.toFixed(2) : best.toFixed(3);
                  return (
                    <div class="meta-best">
                      <Stat
                        size="md"
                        tone="best"
                        label={`best ${mk.replace(/_/g, " ")}`}
                        value={fmtBest}
                      />
                    </div>
                  );
                })()}

                {/* Conclusion */}
                {idea.conclusion && (
                  <div class="meta-conclusion">"{idea.conclusion}"</div>
                )}
              </div>

              {/* Description — below meta so stats are visible first */}
              <Description text={idea.description} />

              {/* View controls — Timeline/Grouped + sort order toggles */}
              {(notes.length > 0 || experiments.length > 0) && (
                <div class="detail-view-controls">
                  <Toggle
                    active={detailTimeline.value}
                    onClick={() => { detailTimeline.value = !detailTimeline.value; }}
                    title={detailTimeline.value ? "Switch to grouped view" : "Switch to timeline view"}
                  >
                    {detailTimeline.value ? "⊞ Timeline" : "⊞ Grouped"}
                  </Toggle>
                  <Toggle
                    active={newestFirst}
                    onClick={() => { detailSortNewest.value = !detailSortNewest.value; }}
                    title="Toggle sort order"
                  >
                    {newestFirst ? "↓ Newest first" : "↑ Oldest first"}
                  </Toggle>
                </div>
              )}

              {/* Content: timeline or normal */}
              {detailTimeline.value ? (
                <div class="tl-list">
                  {tlItems.map((item, idx) => (
                    <div key={idx} class="tl-entry">
                      <div class="tl-marker">
                        <span class={`tl-dot tl-dot-${item.kind === "note" ? (item.note.level || "note") : item.exp.status}`} />
                        <span class="tl-ts">{formatTime(item.kind === "note" ? item.note.created_at : item.exp.created_at)}</span>
                        <span class="tl-rule" />
                        <span class={`tl-kind tl-kind-${item.kind === "note" ? (item.note.level || "note") : item.exp.status}`}>
                          {item.kind === "note" ? (item.note.level || "note") : `exp/${item.exp.label || item.exp.id}`}
                        </span>
                      </div>
                      {item.kind === "note" ? (
                        <div class={`tl-content note-item ${item.note.level}`}>
                          {item.note.text}
                        </div>
                      ) : (
                        <div class="tl-content">
                          <ExperimentItem
                            exp={item.exp}
                            progress={progressData[String(item.exp.id)]}
                            onShowLog={() => openLog(item.exp)}
                            onShowScript={() => openScript(item.exp)}
                            onShowOutput={() => openOutput(item.exp)}
                          />
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <>
                  {sortedNotes.length > 0 && (
                    <section class="detail-section">
                      <div class="detail-section-head">
                        <Eyebrow>Notes</Eyebrow>
                        <span class="detail-section-count">{sortedNotes.length}</span>
                      </div>
                      <div class="note-list">
                        {sortedNotes.map((note, i) => (
                          <div key={i} class={`note-item ${note.level}`}>
                            <div class="note-meta">
                              <span class={`note-dot note-dot-${note.level || "note"}`} />
                              <span class="note-level">{note.level || "note"}</span>
                              <span class="note-ts">{formatTime(note.created_at)}</span>
                            </div>
                            <div class="note-text">{note.text}</div>
                          </div>
                        ))}
                      </div>
                    </section>
                  )}
                  {sortedExps.length > 0 && (
                    <section class="detail-section">
                      <div class="detail-section-head">
                        <Eyebrow>Experiments</Eyebrow>
                        <span class="detail-section-count">{sortedExps.length}</span>
                      </div>
                      <div class="exp-list">
                        {sortedExps.map((exp) => (
                          <ExperimentItem
                            key={exp.id}
                            exp={exp}
                            progress={progressData[String(exp.id)]}
                            onShowLog={() => openLog(exp)}
                            onShowScript={() => openScript(exp)}
                            onShowOutput={() => openOutput(exp)}
                          />
                        ))}
                      </div>
                    </section>
                  )}
                </>
              )}
            </>
          )}
        </div>
      </div>

      {/* Log Lightbox */}
      {logExp && (
        <Lightbox
          title={`Log — exp/${logExp.label || logExp.id}: ${logExp.description}`}
          onClose={() => { setLogExp(null); setHash({ view: null, exp: null }); }}
          bodyRef={logBodyRef}
          onBodyScroll={(e) => {
            const el = e.currentTarget as HTMLDivElement;
            const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
            setLogFollowing(atBottom);
          }}
          toolbar={
            <Toggle
              active={logFollowing}
              onClick={() => {
                const next = !logFollowing;
                setLogFollowing(next);
                if (next && logBodyRef.current) {
                  logBodyRef.current.scrollTop = logBodyRef.current.scrollHeight;
                }
              }}
            >
              {logFollowing ? "↓ Following" : "↓ Follow"}
            </Toggle>
          }
        >
          {logLoading && <div class="lightbox-loading">Loading…</div>}
          {logContent !== null && <pre>{logContent || "(empty)"}</pre>}
        </Lightbox>
      )}

      {/* Script Lightbox */}
      {scriptExp && (
        <Lightbox
          title={`Script — exp/${scriptExp.label || scriptExp.id}`}
          onClose={() => { setScriptExp(null); setHash({ view: null, exp: null }); }}
        >
          {scriptLoading && <div class="lightbox-loading">Loading…</div>}
          {scriptContent !== null && (
            <pre dangerouslySetInnerHTML={{ __html: colorizeScript(scriptContent || "(empty)") }} />
          )}
        </Lightbox>
      )}

      {/* Output Lightbox */}
      {outputExp && (() => {
        const linked = _outputLinkedTop;
        const linkedName = linked ? (linked.path.split("/").pop() || linked.path) : "";
        const title = linked
          ? `Output — exp/${outputExp.label || outputExp.id} › ${linkedName}`
          : `Output — exp/${outputExp.label || outputExp.id}`;
        return (
          <Lightbox
            title={title}
            onClose={() => { setOutputExp(null); setHash({ view: null, exp: null }); }}
            bodyRef={outputBodyRef}
            onBodyScroll={(e) => {
              const el = e.currentTarget as HTMLDivElement;
              const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
              setOutputFollowing(atBottom);
            }}
            toolbar={
              <>
                {linked && (
                  <Toggle
                    active={false}
                    onClick={() => {
                      outputDetailsRef.current = new Map();
                      // Restore the parent's scroll position after the
                      // content swap re-renders.
                      pendingScrollRef.current = { kind: "top", value: linked.parentScroll };
                      setOutputFileStack((s) => s.slice(0, -1));
                    }}
                    title={linked.path}
                  >
                    ← Back
                  </Toggle>
                )}
                <Toggle
                  active={false}
                  onClick={() => {
                    // Force a fresh fetch of whatever's currently displayed.
                    // Reset script-execution memory so widgets re-init against
                    // the new DOM, and clear <details> open-state memory so
                    // it tracks new content correctly.
                    executedScriptsRef.current = new Set();
                    outputDetailsRef.current = new Map();
                    if (linked) {
                      const target = linked.path;
                      setOutputFileLoading(true);
                      fetch(`/api/v1/files/${target}`, { cache: "no-cache" })
                        .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`${r.status}`))))
                        .then((content) => {
                          setOutputFileStack((s) => {
                            if (s.length === 0) return s;
                            const next = s.slice(0, -1);
                            next.push({ ...s[s.length - 1], content });
                            return next;
                          });
                        })
                        .catch(() => { /* keep current content on failure */ })
                        .finally(() => setOutputFileLoading(false));
                    } else {
                      fetchOutputContent(outputExp);
                    }
                  }}
                  title="Re-fetch the file from disk"
                >
                  ↻ Refresh
                </Toggle>
                {!linked && (
                  <Toggle
                    active={outputFollowing}
                    onClick={() => {
                      const next = !outputFollowing;
                      setOutputFollowing(next);
                      if (next && outputBodyRef.current) {
                        outputBodyRef.current.scrollTop = outputBodyRef.current.scrollHeight;
                      }
                    }}
                  >
                    {outputFollowing ? "↓ Following" : "↓ Follow"}
                  </Toggle>
                )}
              </>
            }
          >
            {(outputLoading || outputFileLoading) && <div class="lightbox-loading">Loading…</div>}
            {displayedContent !== null && (
              displayedContent.startsWith("(") ? (
                <div class="lightbox-loading lightbox-loading--italic">{displayedContent}</div>
              ) : (
                <div
                  class="md-output"
                  dangerouslySetInnerHTML={{
                    __html: displayedFormat === "html"
                      ? displayedContent
                      : renderMarkdown(displayedContent, displayedBasePath),
                  }}
                />
              )
            )}
          </Lightbox>
        );
      })()}

      {/* Diff Lightbox */}
      {diffOpen && idea && (
        <Lightbox
          title={`Diff — ${idea.branch}`}
          onClose={() => { setDiffOpen(false); setHash({ view: null, exp: null }); }}
          toolbar={
            <Toggle active={diffUseMain} onClick={toggleDiffBase} title="Diff against main instead of the parent branch">
              vs main
            </Toggle>
          }
        >
          {diffLoading && <div class="lightbox-loading">Loading diff…</div>}
          {diffData && (
            <>
              <pre class="diff-stat-pre">{diffData.stat || "No changes"}</pre>
              {diffData.diff && (
                <pre dangerouslySetInnerHTML={{ __html: colorizeDiff(diffData.diff) }} />
              )}
            </>
          )}
        </Lightbox>
      )}
    </>
  );
}

function colorizeDiff(diff: string): string {
  return diff
    .split("\n")
    .map((line) => {
      const esc = escapeHtml(line);
      if (line.startsWith("+++") || line.startsWith("---")) return `<span class="diff-file">${esc}</span>`;
      if (line.startsWith("@@")) return `<span class="diff-hunk">${esc}</span>`;
      if (line.startsWith("+")) return `<span class="diff-add">${esc}</span>`;
      if (line.startsWith("-")) return `<span class="diff-del">${esc}</span>`;
      return esc;
    })
    .join("\n");
}

function colorizeScript(script: string): string {
  return script
    .split("\n")
    .map((line) => {
      const esc = escapeHtml(line);
      if (line.startsWith("#!")) return `<span class="sh-shebang">${esc}</span>`;
      const trimmed = line.trimStart();
      if (trimmed.startsWith("#")) return `<span class="sh-comment">${esc}</span>`;
      if (trimmed.startsWith("set ")) return `<span class="sh-directive">${esc}</span>`;
      if (/^\s*[A-Z_][A-Z0-9_]*=/.test(line)) return `<span class="sh-var">${esc}</span>`;
      if (/^\s*(export|source|cd)\s/.test(line)) return `<span class="sh-builtin">${esc}</span>`;
      return esc;
    })
    .join("\n");
}

function resolveUrl(url: string, basePath: string): string {
  if (!url || /^(https?:|data:|\/)/i.test(url)) return url;
  return `/api/v1/files/${basePath}/${url}`;
}

// Inline HTML tags that pass through (sanitized) inside paragraphs.
// Keep this list conservative: text/styling only — nothing that can load
// remote content or run scripts.
const INLINE_HTML_TAGS = new Set([
  "span", "mark", "kbd", "samp", "var", "sub", "sup", "small",
  "abbr", "dfn", "time", "q", "cite", "code",
  "em", "strong", "i", "b", "u", "s", "strike", "del", "ins",
  "a", "br", "wbr",
]);

// Attributes allowed on any inline tag. `href` is permitted on <a> only,
// gated by protocol whitelist below.
const SAFE_INLINE_ATTRS = new Set([
  "class", "id", "title", "style", "role", "lang", "dir",
]);

// Lazy-load mermaid.js the first time we see a mermaid block. ~600 KB —
// not worth bundling for users who never use mermaid. Stored on a module-
// level promise so concurrent calls share one fetch.
let _mermaidPromise: Promise<any> | null = null;
function ensureMermaid(): Promise<any> {
  if (_mermaidPromise) return _mermaidPromise;
  if ((window as any).mermaid) {
    const m = (window as any).mermaid;
    _mermaidPromise = Promise.resolve(m);
    return _mermaidPromise;
  }
  _mermaidPromise = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js";
    s.onload = () => {
      const m = (window as any).mermaid;
      try {
        m.initialize({
          startOnLoad: false,
          theme: "dark",
          securityLevel: "loose",
        });
      } catch { /* ignore double-init */ }
      resolve(m);
    };
    s.onerror = () => reject(new Error("failed to load mermaid"));
    document.head.appendChild(s);
  });
  return _mermaidPromise;
}

function _safeHrefValue(value: string): string | null {
  const v = value.trim().toLowerCase();
  if (v.startsWith("http://") || v.startsWith("https://") ||
      v.startsWith("/") || v.startsWith("#") || v.startsWith("mailto:")) {
    return value;
  }
  return null;
}

function sanitizeInlineHtmlTag(rawTag: string): string {
  // Match: opening/closing/void tag with optional attributes.
  const m = rawTag.match(/^<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^>]*?)?)(\s*\/)?>$/);
  if (!m) return "";
  const [, slash, tagRaw, attrsRaw, selfCloseSlash] = m;
  const tag = tagRaw.toLowerCase();
  if (!INLINE_HTML_TAGS.has(tag)) return "";
  if (slash) return `</${tag}>`;

  const cleaned: string[] = [];
  if (attrsRaw) {
    const attrRe = /([a-zA-Z-][\w-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
    let am: RegExpExecArray | null;
    while ((am = attrRe.exec(attrsRaw)) !== null) {
      const name = am[1].toLowerCase();
      const rawValue = am[2] ?? am[3] ?? am[4] ?? "";
      if (name.startsWith("on")) continue;  // drop event handlers
      const isSafe = SAFE_INLINE_ATTRS.has(name)
        || name.startsWith("data-")
        || (name === "href" && tag === "a");
      if (!isSafe) continue;
      let value: string | null = rawValue;
      if (name === "href") value = _safeHrefValue(rawValue);
      if (value === null) continue;
      // Escape <, >, and " so the attribute value can't break out.
      const safe = value.replace(/[<>"]/g, (c) =>
        ({ "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
      cleaned.push(`${name}="${safe}"`);
    }
  }
  // External-link hardening: add target+rel when <a href="..."> is present.
  if (tag === "a" && cleaned.some((a) => a.startsWith("href="))) {
    if (!cleaned.some((a) => a.startsWith("target="))) cleaned.push('target="_blank"');
    if (!cleaned.some((a) => a.startsWith("rel="))) cleaned.push('rel="noopener noreferrer"');
  }
  const attrStr = cleaned.length ? " " + cleaned.join(" ") : "";
  const close = selfCloseSlash ? " /" : "";
  return `<${tag}${attrStr}${close}>`;
}

function inlineMd(raw: string, basePath = ""): string {
  const saved: string[] = [];
  const save = (html: string) => { const idx = saved.length; saved.push(html); return `\x00S${idx}\x00`; };

  // Extract code, images, and links BEFORE italic processing so URLs are never mangled
  let s = raw
    .replace(/`([^`]+)`/g, (_, code) => save(`<code class="md-ic">${escapeHtml(code)}</code>`))
    .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_, alt, url) =>
      save(`<img src="${escapeHtml(resolveUrl(url, basePath))}" alt="${escapeHtml(alt)}" class="md-img" />`))
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, text, url) =>
      save(`<a href="${escapeHtml(resolveUrl(url, basePath))}" target="_blank" rel="noopener noreferrer">${escapeHtml(text)}</a>`));

  // Extract whitelisted inline HTML tags so they survive the escape step.
  // Non-whitelisted / malformed matches fall through and get escaped normally.
  s = s.replace(/<\/?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*?)?\s*\/?>/g, (match) => {
    const nameMatch = match.match(/^<\/?([a-zA-Z][a-zA-Z0-9]*)/);
    if (!nameMatch || !INLINE_HTML_TAGS.has(nameMatch[1].toLowerCase())) return match;
    const clean = sanitizeInlineHtmlTag(match);
    return clean ? save(clean) : match;
  });

  // Escape remaining HTML, then apply text formatting
  s = escapeHtml(s);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  s = s.replace(/_([^_\n]+)_/g, "<em>$1</em>");

  // Repeatedly substitute placeholders until none remain — a saved item
  // (e.g. a link) can contain another placeholder (e.g. a code span used
  // as the link text), so a single pass leaves the inner one stranded.
  // Function callback also avoids the `$&` / `$1` replacement-string
  // gotcha when saved HTML contains literal "$".
  for (let _i = 0; _i < 10; _i++) {
    const next = s.replace(/\x00S(\d+)\x00/g, (_, idx) => saved[parseInt(idx)] ?? "");
    if (next === s) break;
    s = next;
  }
  return s;
}

// Block-level HTML tags that pass through unescaped (GFM raw HTML blocks).
// Allows <details>/<summary> and common structural elements to render natively.
const HTML_PASSTHROUGH = new Set([
  "details", "summary",
  "div", "section", "article", "aside", "figure", "figcaption",
  "table", "thead", "tbody", "tfoot", "tr", "th", "td", "colgroup", "col",
  "p", "pre", "blockquote",
  "ul", "ol", "li", "dl", "dt", "dd",
  "h1", "h2", "h3", "h4", "h5", "h6",
  "hr",
  "br", "wbr",
  // Media + interactive — agent-generated outputs frequently embed
  // self-contained widgets (animation viewers, image galleries, etc.).
  "img", "audio", "video", "source", "picture", "iframe",
  "button", "input", "select", "option", "textarea", "label",
  "fieldset", "legend", "form", "progress", "meter",
  "canvas", "svg",
  // Inline scripts/styles for the widgets above.
  "script", "style", "noscript",
]);

// Tags whose content can span multiple lines and shouldn't be reformatted
// (paragraph-wrapped) by the line-by-line markdown processor.
const MULTILINE_PASSTHROUGH = new Set(["pre", "script", "style", "textarea", "svg"]);

/** GFM pipe-table helpers. */
function _isPipeTableRow(line: string): boolean {
  const t = line.trim();
  // Must contain at least one pipe and at least one cell. Leading/trailing
  // pipes optional per spec but we require leading | here to avoid eating
  // arbitrary text containing a pipe.
  return t.startsWith("|") && t.length > 1 && t.includes("|", 1);
}

function _isPipeTableSeparator(line: string): boolean {
  const t = line.trim();
  if (!t.startsWith("|")) return false;
  const inner = t.replace(/^\|/, "").replace(/\|$/, "");
  const cells = inner.split("|");
  if (cells.length === 0) return false;
  return cells.every((c) => /^\s*:?-{2,}:?\s*$/.test(c));
}

function _splitTableRow(line: string): string[] {
  const t = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  // Split on unescaped pipes. \| represents a literal pipe within a cell.
  const out: string[] = [];
  let cur = "";
  for (let i = 0; i < t.length; i++) {
    const ch = t[i];
    if (ch === "\\" && t[i + 1] === "|") { cur += "|"; i++; continue; }
    if (ch === "|") { out.push(cur.trim()); cur = ""; continue; }
    cur += ch;
  }
  out.push(cur.trim());
  return out;
}

function _parseTableAligns(separator: string): Array<"left" | "right" | "center" | null> {
  const t = separator.trim().replace(/^\|/, "").replace(/\|$/, "");
  return t.split("|").map((c) => {
    const s = c.trim();
    const left = s.startsWith(":");
    const right = s.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return null;
  });
}

function _renderTable(
  headers: string[],
  aligns: Array<"left" | "right" | "center" | null>,
  body: string[][],
  basePath: string,
): string {
  const styleFor = (idx: number) => {
    const a = aligns[idx];
    return a ? ` style="text-align:${a}"` : "";
  };
  const ths = headers
    .map((h, idx) => `<th${styleFor(idx)}>${inlineMd(h, basePath)}</th>`)
    .join("");
  const trs = body
    .map((row) => {
      const tds = row
        .map((c, idx) => `<td${styleFor(idx)}>${inlineMd(c, basePath)}</td>`)
        .join("");
      return `<tr>${tds}</tr>`;
    })
    .join("");
  return `<table class="md-table"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
}

/** GitHub-style slug from a heading's raw markdown text. Used to give
 *  every heading an id so `[link](#section)` works. Strips inline HTML
 *  tags and markdown emphasis chars first. */
function _slugifyHeading(text: string): string {
  return text
    .replace(/<[^>]+>/g, "")        // strip inline HTML
    .replace(/[`*_~]/g, "")          // strip markdown emphasis
    .toLowerCase()
    .replace(/[^\w\s-]/g, "")        // keep word chars, spaces, hyphens
    .trim()
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-");
}

/** Render a flat list of {indent, text} items into nested <ul><li>…</li></ul>.
 *  Each unique indent value maps to a level, so mixed 2/4-space conventions
 *  still work. Nested <ul>s are placed inside the parent <li> per HTML spec.
 */
function _renderNestedList(
  items: Array<{ indent: number; text: string }>,
  basePath: string,
): string {
  if (items.length === 0) return "";
  const uniqueIndents = Array.from(new Set(items.map((it) => it.indent))).sort(
    (a, b) => a - b,
  );
  const level = new Map(uniqueIndents.map((v, i) => [v, i] as const));
  let html = "";
  let depth = -1;
  for (const it of items) {
    const lvl = level.get(it.indent) || 0;
    if (lvl > depth) {
      while (depth < lvl) {
        html += '<ul class="md-ul">';
        depth++;
      }
    } else if (lvl < depth) {
      html += "</li>";
      while (depth > lvl) {
        html += "</ul></li>";
        depth--;
      }
    } else {
      html += "</li>";
    }
    html += `<li>${inlineMd(it.text, basePath)}`;
  }
  html += "</li>";
  while (depth > 0) {
    html += "</ul></li>";
    depth--;
  }
  html += "</ul>";
  return html;
}

function renderMarkdown(md: string, basePath = ""): string {
  // Extract fenced code blocks first. The language hint after ``` controls
  // how we emit the block: `mermaid` becomes a <pre class="mermaid"> that
  // the post-render effect lazy-loads mermaid.js to render into an SVG;
  // anything else stays a regular code block.
  const blocks: string[] = [];
  let s = md.replace(/```([^\n]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const idx = blocks.length;
    const trimmedLang = (lang || "").trim().toLowerCase();
    if (trimmedLang === "mermaid") {
      blocks.push(`<pre class="mermaid">${escapeHtml(code.trimEnd())}</pre>`);
    } else {
      blocks.push(`<pre class="md-code"><code>${escapeHtml(code.trimEnd())}</code></pre>`);
    }
    return `\x00B${idx}\x00`;
  });

  const out: string[] = [];
  const lines = s.split("\n");
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Restore code block placeholder
    if (/^\x00B\d+\x00$/.test(line.trim())) {
      out.push(line.trim());
      i++;
      continue;
    }

    // Raw HTML block — lines whose first token is an allowed tag pass through unescaped.
    const htmlTag = line.trim().match(/^<\/?([a-zA-Z][a-zA-Z0-9-]*)/);
    if (htmlTag && HTML_PASSTHROUGH.has(htmlTag[1].toLowerCase())) {
      const tagName = htmlTag[1].toLowerCase();
      const isOpening = !line.trim().startsWith("</");
      // For tags that wrap literal multi-line content (<pre>, <script>,
      // <style>, <textarea>, <svg>): if opened on this line and not closed
      // on the same line, consume subsequent lines verbatim until the
      // matching close. Without this, inner lines (multi-line JSON inside
      // <pre>, JS source inside <script>, etc.) get paragraph-wrapped.
      if (isOpening && MULTILINE_PASSTHROUGH.has(tagName)) {
        const closeRe = new RegExp(`</${tagName}\\s*>`, "i");
        if (!closeRe.test(line)) {
          const block = [line];
          i++;
          while (i < lines.length) {
            block.push(lines[i]);
            if (closeRe.test(lines[i])) { i++; break; }
            i++;
          }
          out.push(block.join("\n"));
          continue;
        }
      }
      out.push(line);
      i++;
      continue;
    }

    // Headings — emit a slugified id so [link](#title) anchors work.
    const hm = line.match(/^(#{1,6})\s+(.*)/);
    if (hm) {
      const lvl = hm[1].length;
      const id = _slugifyHeading(hm[2]);
      const idAttr = id ? ` id="${escapeHtml(id)}"` : "";
      out.push(`<h${lvl}${idAttr} class="md-h${lvl}">${inlineMd(hm[2], basePath)}</h${lvl}>`);
      i++;
      continue;
    }

    // Horizontal rule
    if (/^[-*]{3,}$/.test(line.trim())) {
      out.push('<hr class="md-hr" />');
      i++;
      continue;
    }

    // GFM pipe table — header row + separator row + body. Standard syntax
    // (leading/trailing pipes optional, GFM allows omitting them but the
    // common case includes them and that's what the agent emits).
    if (
      _isPipeTableRow(line) &&
      i + 1 < lines.length &&
      _isPipeTableSeparator(lines[i + 1])
    ) {
      const headers = _splitTableRow(line);
      const aligns = _parseTableAligns(lines[i + 1]);
      i += 2;
      const body: string[][] = [];
      while (i < lines.length && _isPipeTableRow(lines[i])) {
        body.push(_splitTableRow(lines[i]));
        i++;
      }
      out.push(_renderTable(headers, aligns, body, basePath));
      continue;
    }

    // List — supports nesting via leading whitespace. Each unique indent
    // value becomes a level (so 2-space and 4-space conventions both work).
    const _listRe = /^(\s*)([-*+]|\d+\.)\s+(.*)$/;
    if (_listRe.test(line)) {
      const items: Array<{ indent: number; text: string }> = [];
      while (i < lines.length) {
        const m = lines[i].match(_listRe);
        if (!m) break;
        items.push({ indent: m[1].length, text: m[3] });
        i++;
      }
      out.push(_renderNestedList(items, basePath));
      continue;
    }

    // Empty line
    if (line.trim() === "") {
      out.push('<div class="md-gap"></div>');
      i++;
      continue;
    }

    // Paragraph
    out.push(`<p class="md-p">${inlineMd(line, basePath)}</p>`);
    i++;
  }

  let html = out.join("\n");
  // Same loop-until-stable + function-callback pattern as inlineMd's
  // saved-restore — defends against $-interpretation in code-block content.
  for (let _i = 0; _i < 10; _i++) {
    const next = html.replace(/\x00B(\d+)\x00/g, (_, idx) => blocks[parseInt(idx)] ?? "");
    if (next === html) break;
    html = next;
  }
  return html;
}

/** Parent-idea link — click navigates + hover-highlights via the shared hook. */
function ParentLink({ pid }: { pid: number }) {
  const nav = useEntityNav(pid);
  return (
    <a
      class={`parent-link${nav.highlighted ? " is-highlighted" : ""}`}
      href="#"
      {...nav.bind}
      onClick={(e) => { e.preventDefault(); nav.bind.onClick(e as unknown as MouseEvent); }}
      title={`Jump to idea #${pid}`}
    >
      #{pid}
    </a>
  );
}

/** Collapsible idea description — auto-open when short, via useDisclosure. */
function Description({ text }: { text: string }) {
  const { open, toggle } = useDisclosure(text.length < 200);
  return (
    <div class="detail-desc-wrap">
      <button
        type="button"
        class={`detail-desc-toggle${open ? " is-open" : ""}`}
        onClick={toggle}
        aria-expanded={open}
      >
        {text.length < 200 ? "Description" : `Description (${text.length} chars)`}
      </button>
      {open && <div class="detail-desc">{text}</div>}
    </div>
  );
}

function ExperimentItem({
  exp,
  progress,
  onShowLog,
  onShowScript,
  onShowOutput,
}: {
  exp: Experiment;
  progress?: Record<string, any>;
  onShowLog: () => void;
  onShowScript: () => void;
  onShowOutput?: () => void;
}) {
  // Runtime-resolved so the colour follows the active theme
  const statusColor = getStatusColor(exp.status);
  const metricKey = selectedMetric.value;
  const highlights = metricKey ? [metricKey] : [];

  // Experiment-header → graph navigation + hover-highlight via the shared hook.
  const nav = useEntityNav(exp.idea_id);

  // Check if this experiment was a global best at the time it ran
  const isMilestone = (() => {
    if (!metricKey || !exp.metrics || typeof exp.metrics[metricKey] !== "number" || exp._running) return false;
    const lower = isLowerBetter(metricKey);
    // Import lazily — avoid circular dep
    try {
      const allExps = allExperiments.value;
      const myVal = exp.metrics[metricKey] as number;
      const earlier = allExps.filter(e => !e._running && e.id < exp.id && e.metrics && typeof e.metrics[metricKey] === "number");
      if (earlier.length === 0) return true; // first experiment with this metric
      const prevBest = earlier.reduce<number>((b, e) => {
        const v = e.metrics![metricKey] as number;
        return lower ? Math.min(b, v) : Math.max(b, v);
      }, lower ? Infinity : -Infinity);
      return lower ? myVal < prevBest : myVal > prevBest;
    } catch { return false; }
  })();

  return (
    <div class={`exp-item${nav.highlighted ? " exp-highlight" : ""}`} data-exp-label={exp.label || exp.id}>
      <div class="exp-header">
        <span class="exp-id" {...nav.bind} title="Scroll to this idea in graph + highlight in charts">
          {isMilestone && <span class="exp-milestone-star" title="New global best at this point">★</span>}
          exp/{exp.label || exp.id}
        </span>
        {/* Primary metric value — most important number at a glance; reads like a <Stat>. */}
        {metricKey && exp.metrics && typeof exp.metrics[metricKey] === "number" && !exp._running && (
          <span class="exp-metric-badge" title={`${metricKey} = ${exp.metrics[metricKey]}`}>
            {(exp.metrics[metricKey] as number).toFixed(
              Math.abs(exp.metrics[metricKey] as number) >= 100 ? 0 :
              Math.abs(exp.metrics[metricKey] as number) >= 1 ? 2 : 3
            )}
          </span>
        )}
        <span dangerouslySetInnerHTML={{ __html: badgeHtml(exp.status, progress?.pct_complete ?? progress?.pct) }} />
      </div>
      <div class="exp-desc">{exp.description}</div>
      {exp.tags && exp.tags.length > 0 && (
        <div class="exp-tags">{exp.tags.map((t) => <span key={t} class="tag-pill">{t}</span>)}</div>
      )}
      {progress && Object.keys(progress).length > 0 ? (
        <JsonView data={progress} label="progress" labelColor={statusColor} startCollapsed />
      ) : exp.status === "running" ? (
        <div class="exp-progress">loading progress...</div>
      ) : null}
      <JsonView data={exp.metrics} label="metrics" highlightKeys={highlights} />
      <JsonView data={exp.meta} label="meta" startCollapsed />
      <div class="exp-timestamps">
        created: {formatTime(exp.created_at)}
        {exp.started_at && <> | started: {formatTime(exp.started_at)}</>}
        {exp.finished_at && <> | finished: {formatTime(exp.finished_at)}</>}
        {exp.runtime && <> | runtime: {exp.runtime}</>}
      </div>
      {exp.status === "running" && exp.started_at && (() => {
        const pct = progress?.pct_complete ?? progress?.pct;
        if (typeof pct !== "number" || pct <= 0) return null;
        const elapsed = (Date.now() - new Date(exp.started_at).getTime()) / 1000;
        const total = elapsed / (pct / 100);
        const remaining = Math.max(0, total - elapsed);
        const eta = new Date(Date.now() + remaining * 1000);
        const fmtRemaining = remaining < 60
          ? `${Math.round(remaining)}s`
          : remaining < 3600
          ? `${Math.round(remaining / 60)}m`
          : `${(remaining / 3600).toFixed(1)}h`;
        return (
          <div class="exp-eta">
            ETA: {fmtRemaining} remaining ({eta.toLocaleTimeString()})
          </div>
        );
      })()}
      <div class="exp-actions">
        {exp.has_output && onShowOutput && (
          <Toggle active={false} onClick={onShowOutput}>Show output</Toggle>
        )}
        <Toggle active={false} onClick={onShowLog}>Show log</Toggle>
        <Toggle active={false} onClick={onShowScript}>Show script</Toggle>
      </div>
    </div>
  );
}
