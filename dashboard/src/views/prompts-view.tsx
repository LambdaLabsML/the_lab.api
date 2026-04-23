import { useEffect, useRef, useState } from "preact/hooks";
import { listPrompts, getPrompt, putPrompt, deletePromptRole } from "../state/api";
import type { PromptMeta } from "../lib/types";
import { formatTime } from "../lib/format";

const ROLE_REGEX = /^[a-z0-9_-]{1,32}$/;

function launchCommand(role: string): string {
  if (role === "default") return "the-lab-agent loop -d 30m";
  return `the-lab-agent --role ${role} loop -d 30m`;
}

function formatSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
}

export function PromptsView() {
  const [roles, setRoles] = useState<PromptMeta[]>([]);
  const [selectedRole, setSelectedRole] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [savedContent, setSavedContent] = useState("");
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied">("idle");
  const [showAddForm, setShowAddForm] = useState(false);
  const [newRoleName, setNewRoleName] = useState("");
  const [addError, setAddError] = useState<string | null>(null);

  const selectedRoleRef = useRef<string | null>(null);
  const contentRef = useRef(content);
  const savedContentRef = useRef(savedContent);
  selectedRoleRef.current = selectedRole;
  contentRef.current = content;
  savedContentRef.current = savedContent;

  const dirty = selectedRole !== null && content !== savedContent;

  async function refreshList(): Promise<PromptMeta[] | null> {
    try {
      const list = await listPrompts();
      setRoles(list);
      setListError(null);
      return list;
    } catch (err) {
      setListError(err instanceof Error ? err.message : String(err));
      return null;
    }
  }

  async function loadRole(role: string) {
    try {
      const res = await getPrompt(role);
      setContent(res.content);
      setSavedContent(res.content);
      setSelectedRole(role);
      setSaveState("idle");
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  // Initial load + polling
  useEffect(() => {
    let cancelled = false;

    (async () => {
      const list = await refreshList();
      if (cancelled || !list) return;
      if (list.length > 0 && selectedRoleRef.current === null) {
        await loadRole(list[0].role);
      }
    })();

    const interval = window.setInterval(async () => {
      if (cancelled) return;
      try {
        const list = await listPrompts();
        if (cancelled) return;
        setRoles(list);
        setListError(null);
      } catch {
        // keep last-good list
      }
    }, 10000);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  function handleSelectRole(role: string) {
    if (role === selectedRole) return;
    if (dirty) {
      const ok = window.confirm(
        "You have unsaved changes. Discard them and switch roles?",
      );
      if (!ok) return;
    }
    loadRole(role);
  }

  async function handleSave() {
    if (selectedRole === null) return;
    setSaveState("saving");
    setError(null);
    try {
      await putPrompt(selectedRole, content);
      setSavedContent(content);
      setSaveState("saved");
      await refreshList();
      window.setTimeout(
        () => setSaveState((prev) => (prev === "saved" ? "idle" : prev)),
        1500,
      );
    } catch (err) {
      setSaveState("error");
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleDelete() {
    if (selectedRole === null || selectedRole === "default") return;
    const ok = window.confirm(
      `Delete role "${selectedRole}"? This cannot be undone.`,
    );
    if (!ok) return;
    try {
      await deletePromptRole(selectedRole);
      setSelectedRole(null);
      setContent("");
      setSavedContent("");
      const list = await refreshList();
      if (list && list.length > 0) {
        await loadRole(list[0].role);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleCopyLaunch() {
    if (selectedRole === null) return;
    const cmd = launchCommand(selectedRole);
    try {
      await navigator.clipboard.writeText(cmd);
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1500);
    } catch {
      // fallback: select via prompt
      window.prompt("Copy launch command:", cmd);
    }
  }

  async function handleAddRole(e: Event) {
    e.preventDefault();
    const name = newRoleName.trim();
    if (!ROLE_REGEX.test(name)) {
      setAddError(
        "Role name must be lowercase letters, digits, dash or underscore (1-32 chars).",
      );
      return;
    }
    if (name === "default") {
      setAddError("\"default\" is reserved.");
      return;
    }
    if (roles.some((r) => r.role === name)) {
      setAddError("Role already exists.");
      return;
    }
    if (dirty) {
      const ok = window.confirm(
        "You have unsaved changes. Discard them and add the new role?",
      );
      if (!ok) return;
    }
    try {
      await putPrompt(name, "");
      setShowAddForm(false);
      setNewRoleName("");
      setAddError(null);
      await refreshList();
      await loadRole(name);
    } catch (err) {
      setAddError(err instanceof Error ? err.message : String(err));
    }
  }

  const saveLabel =
    saveState === "saving"
      ? "Saving..."
      : saveState === "saved"
      ? "Saved"
      : saveState === "error"
      ? "Error"
      : dirty
      ? "Save"
      : "Saved";

  return (
    <div id="prompts-container">
      <aside class="prompts-sidebar">
        <div class="prompts-sidebar-header">
          <h3>Roles</h3>
        </div>
        {listError && <div class="prompts-error">{listError}</div>}
        <div class="prompts-role-list">
          {roles.map((r) => (
            <button
              key={r.role}
              class={`prompts-role-row${selectedRole === r.role ? " selected" : ""}`}
              onClick={() => handleSelectRole(r.role)}
            >
              <div class="prompts-role-name">
                {r.role}
                {r.role === "default" && <span class="prompts-role-badge">default</span>}
              </div>
              <div class="prompts-role-meta">
                {formatSize(r.size)} · {formatTime(r.updated_at)}
              </div>
            </button>
          ))}
          {roles.length === 0 && !listError && (
            <div class="prompts-empty">No prompts found.</div>
          )}
        </div>
        {showAddForm ? (
          <form class="prompts-add-form" onSubmit={handleAddRole}>
            <input
              type="text"
              placeholder="role-name"
              value={newRoleName}
              onInput={(e) => {
                setNewRoleName((e.target as HTMLInputElement).value);
                setAddError(null);
              }}
              autoFocus
            />
            {addError && <div class="prompts-add-error">{addError}</div>}
            <div class="prompts-add-actions">
              <button type="submit" class="prompts-btn-primary">Add</button>
              <button
                type="button"
                class="prompts-btn"
                onClick={() => {
                  setShowAddForm(false);
                  setNewRoleName("");
                  setAddError(null);
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <button
            class="prompts-add-btn"
            onClick={() => setShowAddForm(true)}
          >
            + Add role
          </button>
        )}
      </aside>

      <main class="prompts-main">
        {selectedRole === null ? (
          <div class="prompts-placeholder">
            Select a role on the left to edit its prompt.
          </div>
        ) : (
          <>
            <div class="prompts-main-header">
              <div class="prompts-main-title">
                <h2>{selectedRole}</h2>
                <div class="prompts-main-subtitle">
                  {selectedRole === "default"
                    ? "Base prompt applied when no --role flag is set."
                    : `Overrides the default prompt when --role ${selectedRole} is used.`}
                </div>
              </div>
              <div class="prompts-main-actions">
                <button
                  class={`prompts-btn${copyState === "copied" ? " copied" : ""}`}
                  onClick={handleCopyLaunch}
                  title={launchCommand(selectedRole)}
                >
                  {copyState === "copied" ? "Copied!" : "Copy launch command"}
                </button>
                {selectedRole !== "default" && (
                  <button class="prompts-btn danger" onClick={handleDelete}>
                    Delete
                  </button>
                )}
                <button
                  class={`prompts-btn primary prompts-save-${saveState}${dirty ? " dirty" : ""}`}
                  onClick={handleSave}
                  disabled={!dirty || saveState === "saving"}
                >
                  {saveLabel}
                </button>
              </div>
            </div>
            {error && <div class="prompts-error">{error}</div>}
            <textarea
              class="prompts-textarea"
              value={content}
              onInput={(e) => setContent((e.target as HTMLTextAreaElement).value)}
              placeholder="# Prompt content (markdown)"
              spellcheck={false}
            />
            <div class="prompts-footer">
              <code>{launchCommand(selectedRole)}</code>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
