/**
 * AgentQuickChat — a one-line composer under the sidebar's Active agents list:
 * pick a recipient (all agents or one) and send a message via the inter-agent
 * inbox (POST /api/v1/messages). Terminal-clean per DESIGN.md: hairline field,
 * ghost button, no boxes.
 */
import { useState } from "preact/hooks";
import { agentStates } from "../../state/agent-activity";
import { agentColor } from "../../lib/colors";
import { sendMessage } from "../../state/api";

export function AgentQuickChat() {
  const [to, setTo] = useState("all");
  const [text, setText] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">("idle");

  const agents = Object.values(agentStates.value)
    .sort((a, b) => Number(b.active) - Number(a.active) || a.agentId.localeCompare(b.agentId));

  async function submit(e: Event) {
    e.preventDefault();
    const body = text.trim();
    if (!body || state === "sending") return;
    setState("sending");
    try {
      await sendMessage(to === "all" ? "all" : `agent:${to}`, body);
      setText("");
      setState("sent");
      window.setTimeout(() => setState((s) => (s === "sent" ? "idle" : s)), 2000);
    } catch {
      setState("error");
    }
  }

  return (
    <form class="aa-chat" onSubmit={submit}>
      <select
        class="aa-chat-to"
        value={to}
        title="Recipient"
        style={to !== "all" ? { color: agentColor(to) } : undefined}
        onChange={(e) => setTo((e.target as HTMLSelectElement).value)}
      >
        <option value="all">@all</option>
        {agents.map((a) => (
          <option key={a.agentId} value={a.agentId}>@{a.agentId}</option>
        ))}
      </select>
      <input
        class="aa-chat-input"
        type="text"
        placeholder="message agents…"
        value={text}
        onInput={(e) => { setText((e.target as HTMLInputElement).value); if (state === "error") setState("idle"); }}
      />
      <button class="ui-btn aa-chat-send" type="submit" disabled={!text.trim() || state === "sending"}
        title="Send (lands in the agents' the-lab messages inbox)">
        {state === "sent" ? "✓" : state === "error" ? "retry" : "send"}
      </button>
    </form>
  );
}
