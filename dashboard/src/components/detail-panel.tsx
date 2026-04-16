import { useEffect, useState, useRef } from "preact/hooks";
import { selectedIdea, selectedMetric } from "../state/settings";
import { scrollToExperiment, runningProgress } from "../state/signals";
import { getIdea, getExperimentProgress, getExperimentLog, getExperimentScript, getIdeaDiff } from "../state/api";
import { formatTime, badgeHtml, escapeHtml } from "../lib/format";
import { navigateToIdea, navigateFromExperiment } from "../lib/navigate";
import { Lightbox } from "./lightbox";
import { JsonView } from "./json-view";
import type { IdeaDetail, Experiment, Note } from "../lib/types";

export function DetailPanel() {
  const ideaId = selectedIdea.value;
  const [idea, setIdea] = useState<IdeaDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const pollRef = useRef<number | null>(null);
  const fetchRef = useRef(0);

  // Lightbox state
  const [logExp, setLogExp] = useState<Experiment | null>(null);
  const [logContent, setLogContent] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const [scriptExp, setScriptExp] = useState<Experiment | null>(null);
  const [scriptContent, setScriptContent] = useState<string | null>(null);
  const [scriptLoading, setScriptLoading] = useState(false);
  const [diffOpen, setDiffOpen] = useState(false);
  const [diffUseMain, setDiffUseMain] = useState(false);
  const [diffData, setDiffData] = useState<{ stat: string; diff: string } | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);

  // Progress data per experiment (keyed by exp id)
  const [progressData, setProgressData] = useState<Record<string, Record<string, any>>>({});

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
    // Wait a tick for DOM to render
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
            // Update global signal so graph + table stay in sync
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

  // Log lightbox
  function openLog(exp: Experiment) {
    setLogExp(exp);
    setLogContent(null);
    setLogLoading(true);
    getExperimentLog(exp.label || exp.id)
      .then((data) => setLogContent(data.log))
      .catch(() => setLogContent("Failed to load log"))
      .finally(() => setLogLoading(false));
  }

  // Script lightbox
  function openScript(exp: Experiment) {
    setScriptExp(exp);
    setScriptContent(null);
    setScriptLoading(true);
    getExperimentScript(exp.label || exp.id)
      .then((data) => setScriptContent(data.script))
      .catch(() => setScriptContent("Failed to load script"))
      .finally(() => setScriptLoading(false));
  }

  // Diff lightbox
  function openDiff(useMain = false) {
    setDiffOpen(true);
    setDiffUseMain(useMain);
    loadDiff(useMain);
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
              <div class="detail-section">
                <div class="label">Description</div>
                <div class="value">{idea.description}</div>
              </div>

              {idea.branch && (
                <div class="detail-section">
                  <div class="label">Branch</div>
                  <div class="value">{idea.branch}</div>
                </div>
              )}

              {idea.parent_ids && idea.parent_ids.length > 0 && (
                <div class="detail-section">
                  <div class="label">Parents</div>
                  <div class="value">
                    {idea.parent_ids.map((pid, i) => (
                      <span key={pid}>
                        {i > 0 && ", "}
                        <a
                          class="parent-link"
                          href="#"
                          onClick={(e) => { e.preventDefault(); navigateToIdea(pid); }}
                          title={`Jump to idea #${pid}`}
                        >
                          #{pid}
                        </a>
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {idea.conclusion && (
                <div class="detail-section">
                  <div class="label">Conclusion</div>
                  <div class="value">{idea.conclusion}</div>
                </div>
              )}

              {idea.branch && (
                <div class="detail-section">
                  <button class="detail-expand-btn" onClick={() => openDiff(false)}>
                    Show branch diff
                  </button>
                </div>
              )}

              {notes.length > 0 && (
                <div class="detail-section">
                  <div class="label">Notes ({notes.length})</div>
                  {notes.map((note, i) => (
                    <div key={i} class={`note-item ${note.level}`}>
                      <div class="note-meta">{note.level} &middot; {formatTime(note.created_at)}</div>
                      {note.text}
                    </div>
                  ))}
                </div>
              )}

              {experiments.length > 0 && (
                <div class="detail-section">
                  <div class="label">Experiments ({experiments.length})</div>
                  {experiments.map((exp) => (
                    <ExperimentItem
                      key={exp.id}
                      exp={exp}
                      progress={progressData[String(exp.id)]}
                      onShowLog={() => openLog(exp)}
                      onShowScript={() => openScript(exp)}
                    />
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Log Lightbox */}
      {logExp && (
        <Lightbox
          title={`Log — exp/${logExp.label || logExp.id}: ${logExp.description}`}
          onClose={() => setLogExp(null)}
        >
          {logLoading && <div style={{ color: "#8b949e" }}>Loading...</div>}
          {logContent !== null && <pre>{logContent || "(empty)"}</pre>}
        </Lightbox>
      )}

      {/* Script Lightbox */}
      {scriptExp && (
        <Lightbox
          title={`Script — exp/${scriptExp.label || scriptExp.id}`}
          onClose={() => setScriptExp(null)}
        >
          {scriptLoading && <div style={{ color: "#8b949e" }}>Loading...</div>}
          {scriptContent !== null && (
            <pre dangerouslySetInnerHTML={{ __html: colorizeScript(scriptContent || "(empty)") }} />
          )}
        </Lightbox>
      )}

      {/* Diff Lightbox */}
      {diffOpen && idea && (
        <Lightbox
          title={`Diff — ${idea.branch}`}
          onClose={() => setDiffOpen(false)}
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
      // Shebang
      if (line.startsWith("#!")) return `<span class="sh-shebang">${esc}</span>`;
      // Comments
      const trimmed = line.trimStart();
      if (trimmed.startsWith("#")) return `<span class="sh-comment">${esc}</span>`;
      // set directives
      if (trimmed.startsWith("set ")) return `<span class="sh-directive">${esc}</span>`;
      // Variable assignments (KEY=value)
      if (/^\s*[A-Z_][A-Z0-9_]*=/.test(line)) return `<span class="sh-var">${esc}</span>`;
      // export/source/cd
      if (/^\s*(export|source|cd)\s/.test(line)) return `<span class="sh-builtin">${esc}</span>`;
      return esc;
    })
    .join("\n");
}

/** Color for running experiment status sub-headline */
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
}: {
  exp: Experiment;
  progress?: Record<string, any>;
  onShowLog: () => void;
  onShowScript: () => void;
}) {
  const statusColor = STATUS_COLORS[exp.status] || "#8b949e";
  const metricKey = selectedMetric.value;
  const highlights = metricKey ? [metricKey] : [];

  return (
    <div class="exp-item" data-exp-label={exp.label || exp.id}>
      <div class="exp-header">
        <span class="exp-id" style={{ cursor: "pointer" }} onClick={() => navigateFromExperiment(exp.idea_id)} title="Scroll to this idea in graph + highlight in charts">
          exp/{exp.label || exp.id}{exp.label && exp.id !== exp.seq ? ` (legacy: #${exp.id})` : ""}
        </span>
        <span dangerouslySetInnerHTML={{ __html: badgeHtml(exp.status, progress?.pct_complete ?? progress?.pct) }} />
      </div>
      <div class="exp-desc">{exp.description}</div>
      {exp.tags && exp.tags.length > 0 && (
        <div>{exp.tags.map((t) => <span key={t} class="tag-pill">{t}</span>)}</div>
      )}
      {/* Progress: show JsonView when data is available, otherwise a loading placeholder for running exps */}
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
        <button class="detail-expand-btn" onClick={onShowLog}>Show log</button>
        <button class="detail-expand-btn" onClick={onShowScript}>Show script</button>
      </div>
    </div>
  );
}
