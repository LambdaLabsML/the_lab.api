import { useEffect, useState, useRef } from "preact/hooks";
import { selectedIdea } from "../state/settings";
import { getIdea, getExperimentProgress, getExperimentLog, getIdeaDiff } from "../state/api";
import { formatTime, badgeHtml, escapeHtml } from "../lib/format";
import type { IdeaDetail, Experiment, Note } from "../lib/types";

export function DetailPanel() {
  const ideaId = selectedIdea.value;
  const [idea, setIdea] = useState<IdeaDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const pollRef = useRef<number | null>(null);
  const fetchRef = useRef(0);

  useEffect(() => {
    if (ideaId === null) {
      setIdea(null);
      setLoading(false);
      return;
    }
    const seq = ++fetchRef.current;
    setLoading(true);
    getIdea(ideaId, true)
      .then((data) => {
        if (fetchRef.current === seq) { setIdea(data); setLoading(false); }
      })
      .catch(() => {
        if (fetchRef.current === seq) { setIdea(null); setLoading(false); }
      });
  }, [ideaId]);

  // Progress polling
  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    if (!idea) return;
    const running = (idea.experiments || []).filter((e) => e.status === "running");
    if (running.length === 0) return;

    function pollProgress() {
      running.forEach((exp) => {
        getExperimentProgress(exp.id).then((data) => {
          const el = document.getElementById(`progress-${exp.id}`);
          if (!el || !data.progress) return;
          const p = data.progress;
          let html = Object.entries(p).map(([k, v]) => `${k}: ${v}`).join(", ");
          const pct = (p as any).pct ?? (p as any).percent ?? (p as any).progress;
          if (typeof pct === "number") {
            html += `<div class="progress-bar"><div class="progress-fill" style="width:${Math.min(100, pct)}%"></div></div>`;
          }
          el.innerHTML = html;
        });
      });
    }

    pollProgress();
    pollRef.current = window.setInterval(pollProgress, 3000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [idea]);

  if (ideaId === null) return null;

  function close() {
    selectedIdea.value = null;
    history.pushState(null, "", "/" + (window.location.pathname.split("/")[1] || "graph"));
  }

  const notes: Note[] = idea?.notes || [];
  const experiments: Experiment[] = idea?.experiments || [];

  return (
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

            {idea.conclusion && (
              <div class="detail-section">
                <div class="label">Conclusion</div>
                <div class="value">{idea.conclusion}</div>
              </div>
            )}

            {idea.branch && <DiffSection ideaId={idea.id} parentIds={idea.parent_ids} />}

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
                  <ExperimentItem key={exp.id} exp={exp} />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// --- Diff Section ---

function DiffSection({ ideaId, parentIds }: { ideaId: number; parentIds?: number[] }) {
  const [open, setOpen] = useState(false);
  const [useMain, setUseMain] = useState(false);
  const [diff, setDiff] = useState<{ stat: string; diff: string } | null>(null);
  const [loading, setLoading] = useState(false);

  function load(base?: string) {
    setLoading(true);
    setDiff(null);
    getIdeaDiff(ideaId, base)
      .then(setDiff)
      .catch(() => setDiff(null))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    if (open) load(useMain ? "main" : undefined);
  }, [open, useMain]);

  if (!open) {
    return (
      <div class="detail-section">
        <button class="detail-expand-btn" onClick={() => setOpen(true)}>
          Show branch diff
        </button>
      </div>
    );
  }

  return (
    <div class="detail-section">
      <div class="label" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>Branch diff</span>
        <label style={{ fontSize: "10px", fontWeight: "normal", textTransform: "none", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={useMain}
            onChange={(e) => setUseMain((e.target as HTMLInputElement).checked)}
          />{" "}
          vs main
        </label>
      </div>
      {loading && <div style={{ color: "#8b949e", fontSize: "11px" }}>Loading diff...</div>}
      {diff && (
        <>
          <pre class="diff-stat">{diff.stat || "No changes"}</pre>
          {diff.diff && (
            <pre class="diff-content" dangerouslySetInnerHTML={{ __html: colorizeDiff(diff.diff) }} />
          )}
        </>
      )}
    </div>
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

// --- Experiment Item with Log ---

function ExperimentItem({ exp }: { exp: Experiment }) {
  const [logOpen, setLogOpen] = useState(false);
  const [log, setLog] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);

  function toggleLog() {
    if (logOpen) {
      setLogOpen(false);
      return;
    }
    setLogOpen(true);
    setLogLoading(true);
    getExperimentLog(exp.id)
      .then((data) => setLog(data.log))
      .catch(() => setLog("Failed to load log"))
      .finally(() => setLogLoading(false));
  }

  return (
    <div class="exp-item">
      <div class="exp-header">
        <span class="exp-id">exp/{exp.id}</span>
        <span dangerouslySetInnerHTML={{ __html: badgeHtml(exp.status) }} />
      </div>
      <div class="exp-desc">{exp.description}</div>
      {exp.tags && exp.tags.length > 0 && (
        <div>
          {exp.tags.map((t) => (
            <span key={t} class="tag-pill">{t}</span>
          ))}
        </div>
      )}
      {exp.status === "running" && (
        <div class="exp-progress" id={`progress-${exp.id}`}>loading progress...</div>
      )}
      {exp.metrics && Object.keys(exp.metrics).length > 0 && (
        <div class="exp-metrics">metrics: {JSON.stringify(exp.metrics)}</div>
      )}
      {exp.meta && Object.keys(exp.meta).length > 0 && (
        <div class="exp-meta">meta: {JSON.stringify(exp.meta)}</div>
      )}
      <div class="exp-timestamps">
        created: {formatTime(exp.created_at)}
        {exp.started_at && <> | started: {formatTime(exp.started_at)}</>}
        {exp.finished_at && <> | finished: {formatTime(exp.finished_at)}</>}
        {exp.runtime && <> | runtime: {exp.runtime}</>}
      </div>
      <button class="detail-expand-btn" onClick={toggleLog}>
        {logOpen ? "Hide log" : "Show log"}
      </button>
      {logOpen && (
        <div class="exp-log">
          {logLoading && <div style={{ color: "#8b949e", fontSize: "11px" }}>Loading...</div>}
          {log !== null && <pre class="exp-log-content">{log || "(empty)"}</pre>}
        </div>
      )}
    </div>
  );
}
