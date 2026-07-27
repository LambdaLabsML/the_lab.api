/**
 * AgentQuickChat — the message composer pinned at the bottom of the activity
 * sidebar. Auto-growing textarea (Enter sends, Shift+Enter = newline) and a
 * split send button: sends to @all by default, the ▾ arrow picks a specific
 * recipient (menu opens upward). Terminal-clean per DESIGN.md.
 */
import { useRef, useState } from "preact/hooks";
import { serverDemoMode } from "../../state/settings";
import { agentStates } from "../../state/agent-activity";
import { agentColor } from "../../lib/colors";
import { useEscape } from "../../lib/hooks/use-key";
import { sendMessage } from "../../state/api";

export function AgentQuickChat() {
  if (serverDemoMode.value) return null; // read-only demo: no messaging

  const [to, setTo] = useState("all");
  const [text, setText] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">("idle");
  const [menuOpen, setMenuOpen] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEscape(() => setMenuOpen(false), menuOpen);

  const agents = Object.values(agentStates.value)
    .sort((a, b) => Number(b.active) - Number(a.active) || a.agentId.localeCompare(b.agentId));

  function autogrow() {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 120)}px`;
  }

  async function submit() {
    const body = text.trim();
    if (!body || state === "sending") return;
    setState("sending");
    try {
      await sendMessage(to === "all" ? "all" : `agent:${to}`, body);
      setText("");
      if (taRef.current) taRef.current.style.height = "auto";
      setState("sent");
      window.setTimeout(() => setState((s) => (s === "sent" ? "idle" : s)), 2000);
    } catch {
      setState("error");
    }
  }

  const toColor = to === "all" ? undefined : agentColor(to);

  return (
    <form class="aa-chat" onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <textarea
        ref={taRef}
        class="aa-chat-input"
        rows={1}
        placeholder={`message ${to === "all" ? "all agents" : `@${to}`}…`}
        value={text}
        onInput={(e) => {
          setText((e.target as HTMLTextAreaElement).value);
          if (state === "error") setState("idle");
          autogrow();
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
        }}
      />
      <div class="aa-chat-sendwrap">
        <button class="ui-btn aa-chat-send" type="submit" disabled={!text.trim() || state === "sending"}
          style={toColor ? { color: toColor } : undefined}
          title={`Send to ${to === "all" ? "all agents" : `@${to}`} (Enter)`}>
          {state === "sent" ? "✓" : state === "error" ? "retry" : to === "all" ? "send" : `@${to}`}
        </button>
        <button class="ui-btn aa-chat-arrow" type="button" aria-label="Choose recipient"
          onClick={() => setMenuOpen((v) => !v)}>▾</button>
        {menuOpen && (
          <div class="aa-chat-menu" role="menu">
            <button type="button" class={`aa-chat-menu-item${to === "all" ? " is-on" : ""}`}
              onClick={() => { setTo("all"); setMenuOpen(false); }}>@all</button>
            {agents.map((a) => (
              <button type="button" key={a.agentId}
                class={`aa-chat-menu-item${to === a.agentId ? " is-on" : ""}`}
                style={{ color: agentColor(a.agentId) }}
                onClick={() => { setTo(a.agentId); setMenuOpen(false); }}>
                @{a.agentId}
              </button>
            ))}
          </div>
        )}
      </div>
    </form>
  );
}
