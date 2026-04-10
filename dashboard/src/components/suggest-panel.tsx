import { useState } from "preact/hooks";
import { allIdeas } from "../state/signals";
import { suggestIdea } from "../state/api";
import { refreshGraphData } from "../state/polling";

interface LinkRow {
  url: string;
  label: string;
}

export function SuggestPanel() {
  const ideas = allIdeas.value;
  const [desc, setDesc] = useState("");
  const [parentId, setParentId] = useState("");
  const [priority, setPriority] = useState<"normal" | "high">("normal");
  const [links, setLinks] = useState<LinkRow[]>([]);
  const [msg, setMsg] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [submitting, setSubmitting] = useState(false);

  function addLink() {
    setLinks([...links, { url: "", label: "" }]);
  }

  function removeLink(i: number) {
    setLinks(links.filter((_, idx) => idx !== i));
  }

  function updateLink(i: number, field: "url" | "label", value: string) {
    const updated = [...links];
    updated[i] = { ...updated[i], [field]: value };
    setLinks(updated);
  }

  async function submit() {
    if (!desc.trim()) return;
    setSubmitting(true);
    setMsg(null);
    try {
      const resources = links
        .filter((l) => l.url.trim())
        .map((l) => ({ url: l.url.trim(), label: l.label.trim() }));
      await suggestIdea({
        description: desc.trim(),
        parent_ids: parentId ? [parseInt(parentId)] : [],
        priority,
        resources,
      });
      setMsg({ type: "success", text: "Idea submitted!" });
      setDesc("");
      setParentId("");
      setPriority("normal");
      setLinks([]);
      refreshGraphData();
    } catch (e) {
      setMsg({ type: "error", text: String(e) });
    } finally {
      setSubmitting(false);
    }
  }

  const ideaList = Object.values(ideas).sort((a, b) => a.id - b.id);

  return (
    <div id="suggest-panel">
      <div class="suggest-form">
            <div class="form-group">
              <span class="form-label">Description</span>
              <textarea
                value={desc}
                onInput={(e) => setDesc((e.target as HTMLTextAreaElement).value)}
                placeholder="Describe your idea..."
              />
            </div>
            <div class="form-group">
              <span class="form-label">Parent idea (optional)</span>
              <select
                value={parentId}
                onChange={(e) =>
                  setParentId((e.target as HTMLSelectElement).value)
                }
              >
                <option value="">None</option>
                {ideaList.map((idea) => (
                  <option key={idea.id} value={String(idea.id)}>
                    #{idea.id}: {idea.description.slice(0, 60)}
                  </option>
                ))}
              </select>
            </div>
            <div class="form-group">
              <span class="form-label">Priority</span>
              <div class="priority-group">
                <label>
                  <input
                    type="radio"
                    name="suggest-priority"
                    value="normal"
                    checked={priority === "normal"}
                    onChange={() => setPriority("normal")}
                  />{" "}
                  Normal
                </label>
                <label>
                  <input
                    type="radio"
                    name="suggest-priority"
                    value="high"
                    checked={priority === "high"}
                    onChange={() => setPriority("high")}
                  />{" "}
                  High
                </label>
              </div>
            </div>
            <div class="form-group">
              <span class="form-label">Links</span>
              {links.map((link, i) => (
                <div class="link-row" key={i}>
                  <input
                    type="url"
                    placeholder="URL"
                    value={link.url}
                    onInput={(e) =>
                      updateLink(i, "url", (e.target as HTMLInputElement).value)
                    }
                  />
                  <input
                    type="text"
                    placeholder="Label (optional)"
                    value={link.label}
                    onInput={(e) =>
                      updateLink(
                        i,
                        "label",
                        (e.target as HTMLInputElement).value
                      )
                    }
                  />
                  <button
                    class="remove-link-btn"
                    onClick={() => removeLink(i)}
                  >
                    &times;
                  </button>
                </div>
              ))}
              <button type="button" class="add-link-btn" onClick={addLink}>
                + Add link
              </button>
            </div>
            <button
              class="suggest-btn"
              onClick={submit}
              disabled={submitting || !desc.trim()}
            >
              {submitting ? "Submitting..." : "Submit idea"}
            </button>
            {msg && (
              <div class={`suggest-msg ${msg.type}`}>{msg.text}</div>
            )}
          </div>
    </div>
  );
}
