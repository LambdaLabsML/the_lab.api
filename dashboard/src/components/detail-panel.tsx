import { useEffect, useState, useRef } from "preact/hooks";
import { selectedIdea } from "../state/settings";
import { getIdea, getExperimentProgress } from "../state/api";
import { formatTime, badgeHtml } from "../lib/format";
import type { IdeaDetail, Experiment, Note } from "../lib/types";

export function DetailPanel() {
  const ideaId = selectedIdea.value;
  const [idea, setIdea] = useState<IdeaDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const pollRef = useRef<number | null>(null);
  const fetchRef = useRef(0); // avoid stale responses from slow requests

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
        if (fetchRef.current === seq) {
          setIdea(data);
          setLoading(false);
        }
      })
      .catch(() => {
        if (fetchRef.current === seq) {
          setIdea(null);
          setLoading(false);
        }
      });
  }, [ideaId]);

  // Progress polling for running experiments
  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    if (!idea) return;
    const running = (idea.experiments || []).filter(
      (e) => e.status === "running"
    );
    if (running.length === 0) return;

    function pollProgress() {
      running.forEach((exp) => {
        getExperimentProgress(exp.id).then((data) => {
          const el = document.getElementById(`progress-${exp.id}`);
          if (!el || !data.progress) return;
          const p = data.progress;
          let html = Object.entries(p)
            .map(([k, v]) => `${k}: ${v}`)
            .join(", ");
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
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [idea]);

  if (ideaId === null) return null;

  function close() {
    selectedIdea.value = null;
    history.pushState(null, "", "/" + (window.location.pathname.split("/")[1] || "dag"));
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
            {idea && (
              <span dangerouslySetInnerHTML={{ __html: badgeHtml(idea.status) }} />
            )}
          </span>
          <span class="close-btn" onClick={close}>
            &times;
          </span>
        </h2>

        {loading && !idea && (
          <div style={{ padding: "20px 0", color: "#8b949e", fontSize: "12px" }}>
            Loading...
          </div>
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

            {notes.length > 0 && (
              <div class="detail-section">
                <div class="label">Notes ({notes.length})</div>
                {notes.map((note, i) => (
                  <div key={i} class={`note-item ${note.level}`}>
                    <div class="note-meta">
                      {note.level} &middot; {formatTime(note.created_at)}
                    </div>
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

function ExperimentItem({ exp }: { exp: Experiment }) {
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
            <span key={t} class="tag-pill">
              {t}
            </span>
          ))}
        </div>
      )}
      {exp.status === "running" && (
        <div class="exp-progress" id={`progress-${exp.id}`}>
          loading progress...
        </div>
      )}
      {exp.metrics && Object.keys(exp.metrics).length > 0 && (
        <div class="exp-metrics">
          metrics: {JSON.stringify(exp.metrics)}
        </div>
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
    </div>
  );
}
