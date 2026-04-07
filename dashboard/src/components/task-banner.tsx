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
    <div id="task-banner" onClick={startEdit} title="Click to edit current task">
      <span class="task-label">Task:</span>
      <span class="task-text">
        {task?.text || <span class="task-empty">No task set — click to add one</span>}
      </span>
    </div>
  );
}
