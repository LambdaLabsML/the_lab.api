import { useEffect, useState, useRef } from "preact/hooks";
import { selectedIdea, selectedMetric, detailTimeline, detailSortNewest } from "../state/settings";
import { scrollToExperiment, runningProgress } from "../state/signals";
import { getIdea, getExperimentProgress, getExperimentLog, getExperimentScript, getExperimentOutput, getIdeaDiff } from "../state/api";
import { formatTime, badgeHtml, escapeHtml } from "../lib/format";
import { navigateToIdea, navigateFromExperiment } from "../lib/navigate";
import { Lightbox } from "./lightbox";
import { JsonView } from "./json-view";
import type { IdeaDetail, Experiment, Note } from "../lib/types";

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

export function DetailPanel() {
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
  const [outputLoading, setOutputLoading] = useState(false);
  const [outputFollowing, setOutputFollowing] = useState(true);
  const outputBodyRef = useRef<HTMLDivElement>(null);
  const outputPollRef = useRef<number | null>(null);

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

  // Scroll to a specific experiment when signaled
  useEffect(() => {
    const label = scrollToExperiment.value;
    if (!label) return;
    requestAnimationFrame(() => {
      const el = document.querySelector(`[data-exp-label="${label}"]`);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add("exp-highlight");
        setTimeout(() => el.classList.remove("exp-highlight"), 2000);
      }
      scrollToExperiment.value = null;
    });
  }, [scrollToExperiment.value, idea]);

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
      .then((data) => { setOutputContent(data.output); setOutputBasePath(data.base_path || ""); setOutputLoading(false); })
      .catch(() => { setOutputContent("(output file not found)"); setOutputLoading(false); });
  }

  // Auto-refresh output when running
  useEffect(() => {
    if (outputPollRef.current) { clearInterval(outputPollRef.current); outputPollRef.current = null; }
    if (!outputExp || outputExp.status !== "running") return;
    outputPollRef.current = window.setInterval(() => fetchOutputContent(outputExp), 5000);
    return () => { if (outputPollRef.current) { clearInterval(outputPollRef.current); outputPollRef.current = null; } };
  }, [outputExp]);

  // Auto-scroll output when following and content updates
  useEffect(() => {
    if (outputFollowing && outputBodyRef.current) {
      outputBodyRef.current.scrollTop = outputBodyRef.current.scrollHeight;
    }
  }, [outputContent]);

  function openOutput(exp: Experiment) {
    setOutputExp(exp);
    setOutputContent(null);
    setOutputLoading(true);
    setOutputFollowing(true);
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
          <div style={{ padding: "20px", color: "#8b949e", fontSize: "12px" }}>
            Select an idea from the graph, timeline, or chart to see details here.
          </div>
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
          <h2>
            <span>
              Idea #{ideaId}{" "}
              {idea && <span dangerouslySetInnerHTML={{ __html: badgeHtml(idea.status) }} />}
            </span>
            <span class="close-btn" onClick={close}>&times;</span>
          </h2>

          {loading && !idea && (
            <div style={{ padding: "20px 0", color: "#8b949e", fontSize: "12px" }}>Loading...</div>
          )}

          {idea && (
            <>
              {/* Description */}
              <div class="detail-desc">{idea.description}</div>

              {/* Compact meta block */}
              <div class="idea-meta-block">

                {/* Row 1: branch + parents + diff */}
                {(idea.branch || (idea.parent_ids && idea.parent_ids.length > 0)) && (
                  <div class="meta-row">
                    {idea.branch && (
                      <>
                        <span class="meta-branch" title={idea.branch}>⎇ {idea.branch}</span>
                        <button class="meta-btn" onClick={() => openDiff(false)}>diff ↗</button>
                      </>
                    )}
                    {idea.parent_ids && idea.parent_ids.length > 0 && (
                      <span class="meta-parents">
                        {idea.parent_ids.map((pid, i) => (
                          <span key={pid}>
                            {i === 0 && <span class="meta-arrow">←</span>}
                            {" "}
                            <a class="parent-link" href="#"
                              onClick={(e) => { e.preventDefault(); navigateToIdea(pid); }}
                              title={`Jump to idea #${pid}`}>#{pid}</a>
                          </span>
                        ))}
                      </span>
                    )}
                  </div>
                )}

                {/* Row 2: experiment stats */}
                {experiments.length > 0 && (
                  <div class="meta-row meta-stats-row">
                    <span class="meta-stat-label">{experiments.length} exp</span>
                    {STATUS_ORDER.filter(s => expByStatus[s]).map(s => (
                      <span key={s} class={`meta-pill meta-pill-${s}`}>{expByStatus[s]} {s}</span>
                    ))}
                  </div>
                )}

                {/* Row 3: note stats */}
                {notes.length > 0 && (
                  <div class="meta-row meta-stats-row">
                    <span class="meta-stat-label">{notes.length} notes</span>
                    {LEVEL_ORDER.filter(l => noteByLevel[l]).map(l => (
                      <span key={l} class={`meta-pill meta-pill-note-${l}`}>{noteByLevel[l]} {l}</span>
                    ))}
                    {noteByLevel["note"] ? <span class="meta-pill meta-pill-note-note">{noteByLevel["note"]} note</span> : null}
                  </div>
                )}

                {/* Conclusion */}
                {idea.conclusion && (
                  <div class="meta-conclusion">"{idea.conclusion}"</div>
                )}
              </div>

              {/* View controls */}
              {(notes.length > 0 || experiments.length > 0) && (
                <div class="detail-view-controls">
                  <button
                    class="view-ctrl-btn vc-active"
                    onClick={() => { detailTimeline.value = !detailTimeline.value; }}
                    title={detailTimeline.value ? "Switch to grouped view" : "Switch to timeline view"}
                  >
                    {detailTimeline.value ? "⊞ Timeline" : "⊞ Grouped"}
                  </button>
                  <button
                    class="view-ctrl-btn"
                    onClick={() => { detailSortNewest.value = !detailSortNewest.value; }}
                    title="Toggle sort order"
                  >
                    {newestFirst ? "↓ Newest first" : "↑ Oldest first"}
                  </button>
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
                    <div class="detail-section">
                      <div class="label">Notes ({sortedNotes.length})</div>
                      {sortedNotes.map((note, i) => (
                        <div key={i} class={`note-item ${note.level}`}>
                          <div class="note-meta">{note.level} &middot; {formatTime(note.created_at)}</div>
                          {note.text}
                        </div>
                      ))}
                    </div>
                  )}
                  {sortedExps.length > 0 && (
                    <div class="detail-section">
                      <div class="label">Experiments ({sortedExps.length})</div>
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
            <button
              class={`follow-btn${logFollowing ? " follow-active" : ""}`}
              onClick={() => {
                const next = !logFollowing;
                setLogFollowing(next);
                if (next && logBodyRef.current) {
                  logBodyRef.current.scrollTop = logBodyRef.current.scrollHeight;
                }
              }}
            >
              {logFollowing ? "↓ Following" : "↓ Follow"}
            </button>
          }
        >
          {logLoading && <div style={{ color: "#8b949e" }}>Loading...</div>}
          {logContent !== null && <pre>{logContent || "(empty)"}</pre>}
        </Lightbox>
      )}

      {/* Script Lightbox */}
      {scriptExp && (
        <Lightbox
          title={`Script — exp/${scriptExp.label || scriptExp.id}`}
          onClose={() => { setScriptExp(null); setHash({ view: null, exp: null }); }}
        >
          {scriptLoading && <div style={{ color: "#8b949e" }}>Loading...</div>}
          {scriptContent !== null && (
            <pre dangerouslySetInnerHTML={{ __html: colorizeScript(scriptContent || "(empty)") }} />
          )}
        </Lightbox>
      )}

      {/* Output Lightbox */}
      {outputExp && (
        <Lightbox
          title={`Output — exp/${outputExp.label || outputExp.id}`}
          onClose={() => { setOutputExp(null); setHash({ view: null, exp: null }); }}
          bodyRef={outputBodyRef}
          onBodyScroll={(e) => {
            const el = e.currentTarget as HTMLDivElement;
            const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
            setOutputFollowing(atBottom);
          }}
          toolbar={
            <button
              class={`follow-btn${outputFollowing ? " follow-active" : ""}`}
              onClick={() => {
                const next = !outputFollowing;
                setOutputFollowing(next);
                if (next && outputBodyRef.current) {
                  outputBodyRef.current.scrollTop = outputBodyRef.current.scrollHeight;
                }
              }}
            >
              {outputFollowing ? "↓ Following" : "↓ Follow"}
            </button>
          }
        >
          {outputLoading && <div style={{ color: "#8b949e" }}>Loading...</div>}
          {outputContent !== null && (
            outputContent.startsWith("(") ? (
              <div style={{ color: "#8b949e", fontStyle: "italic" }}>{outputContent}</div>
            ) : (
              <div class="md-output" dangerouslySetInnerHTML={{ __html: renderMarkdown(outputContent, outputBasePath) }} />
            )
          )}
        </Lightbox>
      )}

      {/* Diff Lightbox */}
      {diffOpen && idea && (
        <Lightbox
          title={`Diff — ${idea.branch}`}
          onClose={() => { setDiffOpen(false); setHash({ view: null, exp: null }); }}
          toolbar={
            <label style={{ cursor: "pointer" }}>
              <input type="checkbox" checked={diffUseMain} onChange={toggleDiffBase} />
              {" "}vs main
            </label>
          }
        >
          {diffLoading && <div style={{ color: "#8b949e" }}>Loading diff...</div>}
          {diffData && (
            <>
              <pre style={{ marginBottom: "8px", color: "#8b949e" }}>{diffData.stat || "No changes"}</pre>
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

  // Escape remaining HTML, then apply text formatting
  s = escapeHtml(s);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  s = s.replace(/_([^_\n]+)_/g, "<em>$1</em>");

  saved.forEach((item, idx) => { s = s.replace(`\x00S${idx}\x00`, item); });
  return s;
}

// Block-level HTML tags that pass through unescaped (GFM raw HTML blocks).
// Allows <details>/<summary> and common structural elements to render natively.
const HTML_PASSTHROUGH = new Set([
  "details", "summary",
  "div", "section", "article", "aside", "figure", "figcaption",
  "table", "thead", "tbody", "tfoot", "tr", "th", "td", "colgroup", "col",
  "br", "wbr",
]);

function renderMarkdown(md: string, basePath = ""): string {
  // Extract fenced code blocks first
  const blocks: string[] = [];
  let s = md.replace(/```[^\n]*\n?([\s\S]*?)```/g, (_, code) => {
    const idx = blocks.length;
    blocks.push(`<pre class="md-code"><code>${escapeHtml(code.trimEnd())}</code></pre>`);
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

    // Raw HTML block — lines whose first token is an allowed tag pass through unescaped
    const htmlTag = line.trim().match(/^<\/?([a-zA-Z][a-zA-Z0-9-]*)/);
    if (htmlTag && HTML_PASSTHROUGH.has(htmlTag[1].toLowerCase())) {
      out.push(line.trim());
      i++;
      continue;
    }

    // Headings
    const hm = line.match(/^(#{1,3})\s+(.*)/);
    if (hm) {
      out.push(`<h${hm[1].length} class="md-h${hm[1].length}">${inlineMd(hm[2], basePath)}</h${hm[1].length}>`);
      i++;
      continue;
    }

    // Horizontal rule
    if (/^[-*]{3,}$/.test(line.trim())) {
      out.push('<hr class="md-hr" />');
      i++;
      continue;
    }

    // List
    if (/^[-*+]\s/.test(line) || /^\d+\.\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && (/^[-*+]\s/.test(lines[i]) || /^\d+\.\s/.test(lines[i]))) {
        const text = lines[i].replace(/^[-*+]\s/, "").replace(/^\d+\.\s/, "");
        items.push(`<li>${inlineMd(text, basePath)}</li>`);
        i++;
      }
      out.push(`<ul class="md-ul">${items.join("")}</ul>`);
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
  blocks.forEach((block, idx) => { html = html.replace(`\x00B${idx}\x00`, block); });
  return html;
}

const STATUS_COLORS: Record<string, string> = {
  running: "#d29922",
  completed: "#3fb950",
  failed: "#f85149",
  cancelled: "#8b949e",
  pending: "#58a6ff",
};

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
  const statusColor = STATUS_COLORS[exp.status] || "#8b949e";
  const metricKey = selectedMetric.value;
  const highlights = metricKey ? [metricKey] : [];

  return (
    <div class="exp-item" data-exp-label={exp.label || exp.id}>
      <div class="exp-header">
        <span class="exp-id" style={{ cursor: "pointer" }} onClick={() => navigateFromExperiment(exp.idea_id)} title="Scroll to this idea in graph + highlight in charts">
          exp/{exp.label || exp.id}
        </span>
        <span dangerouslySetInnerHTML={{ __html: badgeHtml(exp.status, progress?.pct_complete ?? progress?.pct) }} />
      </div>
      <div class="exp-desc">{exp.description}</div>
      {exp.tags && exp.tags.length > 0 && (
        <div>{exp.tags.map((t) => <span key={t} class="tag-pill">{t}</span>)}</div>
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
          <button class="detail-expand-btn" onClick={onShowOutput}>Show output</button>
        )}
        <button class="detail-expand-btn" onClick={onShowLog}>Show log</button>
        <button class="detail-expand-btn" onClick={onShowScript}>Show script</button>
      </div>
    </div>
  );
}
