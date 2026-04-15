/**
 * Metrics & Notes view — shows all metrics across experiments and all notes
 * across ideas in one place, with filtering and sorting.
 */
import { useState, useMemo } from "preact/hooks";
import { allExperiments, allIdeas } from "../state/signals";
import { navigateToIdea } from "../lib/navigate";
import { formatTime } from "../lib/format";
import { getIdea } from "../state/api";

export function MetricsNotesView() {
  const [tab, setTab] = useState<"metrics" | "notes">("metrics");

  return (
    <div class="metrics-notes-view" style={{ padding: "12px", overflow: "auto", height: "100%" }}>
      <div style={{ display: "flex", gap: "8px", marginBottom: "12px" }}>
        <button
          class={`chart-toggle-btn${tab === "metrics" ? " active" : ""}`}
          onClick={() => setTab("metrics")}
        >
          Metrics
        </button>
        <button
          class={`chart-toggle-btn${tab === "notes" ? " active" : ""}`}
          onClick={() => setTab("notes")}
        >
          Notes
        </button>
      </div>
      {tab === "metrics" ? <MetricsTable /> : <NotesTable />}
    </div>
  );
}

function MetricsTable() {
  const experiments = allExperiments.value;
  const ideas = allIdeas.value;
  const [sortKey, setSortKey] = useState<string>("id");
  const [sortAsc, setSortAsc] = useState(true);
  const [filter, setFilter] = useState("");

  // Collect all metric keys across experiments
  const metricKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const exp of experiments) {
      if (exp.metrics) {
        for (const k of Object.keys(exp.metrics)) keys.add(k);
      }
    }
    return [...keys].sort();
  }, [experiments]);

  // Filter metric keys
  const filteredKeys = filter
    ? metricKeys.filter((k) => k.toLowerCase().includes(filter.toLowerCase()))
    : metricKeys;

  // Build rows: one per experiment, columns = metric keys
  const rows = useMemo(() => {
    return experiments
      .filter((e) => e.metrics && Object.keys(e.metrics).length > 0)
      .map((e) => ({
        id: e.label || e.id,
        idea_id: e.idea_id,
        idea_desc: ideas[e.idea_id]?.description || "",
        status: e.status,
        metrics: e.metrics || {},
      }))
      .sort((a, b) => {
        if (sortKey === "id") {
          return sortAsc
            ? String(a.id).localeCompare(String(b.id))
            : String(b.id).localeCompare(String(a.id));
        }
        const va = a.metrics[sortKey] ?? -Infinity;
        const vb = b.metrics[sortKey] ?? -Infinity;
        return sortAsc ? va - vb : vb - va;
      });
  }, [experiments, ideas, sortKey, sortAsc]);

  function toggleSort(key: string) {
    if (sortKey === key) {
      setSortAsc(!sortAsc);
    } else {
      setSortKey(key);
      setSortAsc(true);
    }
  }

  const visibleKeys = filteredKeys.slice(0, 20); // cap columns for performance

  return (
    <div>
      <div style={{ marginBottom: "8px", display: "flex", gap: "8px", alignItems: "center" }}>
        <input
          type="text"
          placeholder="Filter metrics..."
          value={filter}
          onInput={(e) => setFilter((e.target as HTMLInputElement).value)}
          style={{
            background: "#0d1117", border: "1px solid #30363d", borderRadius: "4px",
            color: "#c9d1d9", padding: "4px 8px", fontSize: "11px", fontFamily: "inherit", width: "200px",
          }}
        />
        <span style={{ color: "#484f58", fontSize: "10px" }}>
          {filteredKeys.length} metrics, {rows.length} experiments
          {filteredKeys.length > 20 && ` (showing first 20 columns)`}
        </span>
      </div>
      <div style={{ overflow: "auto", maxHeight: "calc(100% - 80px)" }}>
        <table style={{ borderCollapse: "collapse", fontSize: "10px", fontFamily: "SF Mono, Fira Code, Consolas, monospace" }}>
          <thead>
            <tr>
              <th class="mn-th" onClick={() => toggleSort("id")} style={{ cursor: "pointer", position: "sticky", left: 0, background: "#161b22", zIndex: 1 }}>
                Exp {sortKey === "id" ? (sortAsc ? "^" : "v") : ""}
              </th>
              <th class="mn-th" style={{ position: "sticky", left: 0, background: "#161b22" }}>Idea</th>
              {visibleKeys.map((k) => (
                <th key={k} class="mn-th" onClick={() => toggleSort(k)} style={{ cursor: "pointer" }}>
                  {k} {sortKey === k ? (sortAsc ? "^" : "v") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td class="mn-td" style={{ position: "sticky", left: 0, background: "#0d1117", zIndex: 1 }}>
                  <a href="#" onClick={(e) => { e.preventDefault(); navigateToIdea(row.idea_id, String(row.id)); }} style={{ color: "#58a6ff", textDecoration: "none" }}>
                    {row.id}
                  </a>
                </td>
                <td class="mn-td" style={{ maxWidth: "150px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={row.idea_desc}>
                  {row.idea_desc.slice(0, 30)}
                </td>
                {visibleKeys.map((k) => {
                  const v = row.metrics[k];
                  return (
                    <td key={k} class="mn-td" style={{ textAlign: "right" }}>
                      {v != null ? (typeof v === "number" ? formatMetricValue(v) : String(v)) : ""}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function NotesTable() {
  const ideas = allIdeas.value;
  const [filter, setFilter] = useState("");
  const [notes, setNotes] = useState<Array<{ idea_id: number; idea_desc: string; text: string; level: string; created_at: string }>>([]);
  const [loaded, setLoaded] = useState(false);

  // Load notes from all ideas
  if (!loaded) {
    setLoaded(true);
    const ideaIds = Object.keys(ideas).map(Number).sort((a, b) => a - b);
    Promise.all(
      ideaIds.map((id) =>
        getIdea(id, true).then((data) =>
          (data.notes || []).map((n: any) => ({
            idea_id: id,
            idea_desc: ideas[id]?.description || "",
            text: n.text || "",
            level: n.level || "observation",
            created_at: n.created_at || "",
          }))
        ).catch(() => [])
      )
    ).then((results) => {
      const all = results.flat().sort((a, b) => b.created_at.localeCompare(a.created_at));
      setNotes(all);
    });
  }

  const filtered = filter
    ? notes.filter((n) =>
        n.text.toLowerCase().includes(filter.toLowerCase()) ||
        n.level.toLowerCase().includes(filter.toLowerCase()) ||
        n.idea_desc.toLowerCase().includes(filter.toLowerCase())
      )
    : notes;

  return (
    <div>
      <div style={{ marginBottom: "8px", display: "flex", gap: "8px", alignItems: "center" }}>
        <input
          type="text"
          placeholder="Filter notes..."
          value={filter}
          onInput={(e) => setFilter((e.target as HTMLInputElement).value)}
          style={{
            background: "#0d1117", border: "1px solid #30363d", borderRadius: "4px",
            color: "#c9d1d9", padding: "4px 8px", fontSize: "11px", fontFamily: "inherit", width: "200px",
          }}
        />
        <span style={{ color: "#484f58", fontSize: "10px" }}>
          {filtered.length} notes across {Object.keys(ideas).length} ideas
        </span>
      </div>
      <div style={{ overflow: "auto", maxHeight: "calc(100% - 80px)" }}>
        {filtered.map((note, i) => (
          <div key={i} style={{
            padding: "6px 8px", marginBottom: "4px", borderRadius: "4px",
            borderLeft: `3px solid ${levelColor(note.level)}`,
            background: "#0d1117", fontSize: "11px",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "2px" }}>
              <span>
                <a href="#" onClick={(e) => { e.preventDefault(); navigateToIdea(note.idea_id); }}
                   style={{ color: "#58a6ff", textDecoration: "none", fontWeight: 600 }}>
                  #{note.idea_id}
                </a>
                {" "}
                <span style={{ color: "#8b949e", fontSize: "10px" }}>{note.level}</span>
              </span>
              <span style={{ color: "#484f58", fontSize: "10px" }}>{formatTime(note.created_at)}</span>
            </div>
            <div style={{ color: "#c9d1d9", lineHeight: 1.4 }}>{note.text}</div>
          </div>
        ))}
        {filtered.length === 0 && notes.length > 0 && (
          <div style={{ color: "#484f58", fontSize: "11px", padding: "8px" }}>No notes match filter.</div>
        )}
        {notes.length === 0 && loaded && (
          <div style={{ color: "#484f58", fontSize: "11px", padding: "8px" }}>Loading notes...</div>
        )}
      </div>
    </div>
  );
}

function levelColor(level: string): string {
  switch (level) {
    case "insight": return "#58a6ff";
    case "milestone": return "#d29922";
    case "debug": return "#f85149";
    default: return "#8b949e";
  }
}

function formatMetricValue(n: number): string {
  if (Number.isInteger(n)) return String(n);
  if (Math.abs(n) < 0.0001 && n !== 0) return n.toExponential(2);
  if (Math.abs(n) >= 1000) return n.toFixed(1);
  return n.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
}
