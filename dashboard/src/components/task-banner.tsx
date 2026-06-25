import { useState, useEffect } from "preact/hooks";

interface Task {
  text: string;
  updated_at: string;
}

export function TaskBanner() {
  const [task, setTask] = useState<Task | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  async function load() {
    try {
      const resp = await fetch("/api/v1/task", { headers: { "X-The-Lab-Source": "dashboard" } });
      if (resp.ok) {
        const data = await resp.json();
        setTask(data);
      }
    } catch { /* ignore */ }
  }

  useEffect(() => {
    load();
    const timer = setInterval(load, 30_000);
    return () => clearInterval(timer);
  }, []);

  async function save() {
    setEditing(false);
    const text = draft.trim();
    try {
      const resp = await fetch("/api/v1/task", {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-The-Lab-Source": "dashboard" },
        body: JSON.stringify({ text }),
      });
      if (resp.ok) {
        const data = await resp.json();
        setTask(data);
      }
    } catch { /* ignore */ }
  }

  function startEdit() {
    setDraft(task?.text || "");
    setEditing(true);
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      save();
    }
    if (e.key === "Escape") {
      setEditing(false);
    }
  }

  if (editing) {
    return (
      <div id="task-banner" class="editing">
        <span class="task-label">Task:</span>
        <input
          class="task-input"
          value={draft}
          onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
          onBlur={save}
          onKeyDown={handleKeyDown}
          placeholder="Set a task for agents (e.g. 'Focus on improving 066 with cascade')"
          autoFocus
        />
      </div>
    );
  }

  return (
    <div id="task-banner">
      <div class="pane-bar">
        <h2 class="pane-bar-title">Task</h2>
        <span class="pane-bar-count">{task?.updated_at ? `updated ${new Date(task.updated_at).toLocaleDateString()}` : "not set"}</span>
        <div class="pane-bar-actions">
          <button class="agents-btn" onClick={startEdit} title="Edit current task">Edit</button>
        </div>
      </div>
      <div style={{ padding: "10px 14px", fontSize: "var(--text-sm)", color: task?.text ? "var(--text)" : "var(--text-faint)", lineHeight: 1.5, whiteSpace: "pre-wrap", cursor: "pointer" }} onClick={startEdit} title="Click to edit">
        {task?.text || "No task set — click Edit to add one"}
      </div>
    </div>
  );
}
