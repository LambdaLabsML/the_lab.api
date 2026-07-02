/**
 * RichText — renders a plain string with entity references swapped for the
 * shared components: exp/129.5 (or a bare 129.5 whose idea exists) → ExpLink,
 * idea/93 / #93 → IdeaLink, @agent or a bare known agent id → AgentPill.
 * Used by message bodies (Messages view, hover preview) and the activity rows,
 * so every mention is hoverable/clickable the same way app-wide.
 */
import type { ComponentChildren } from "preact";
import { ExpLink } from "./exp-link";
import { IdeaLink } from "./idea-link";
import { AgentPill } from "./agent-pill";
import { allIdeas } from "../state/signals";
import { agentStates } from "../state/agent-activity";

const TOKEN_RE =
  /(exp\/\d+\.[\w.-]+|idea\/\d+|#\d+|@[a-z0-9]{3,12}\b|\b\d+\.\d+\b|\b[a-z][a-z0-9]{4}\b)/g;

/** Strip an agent's own "[from → to]" routing prefix (the UI shows pills). */
export function stripRouting(text: string): string {
  return text.replace(/^\s*\[[^\]]{0,60}\]\s*/, "");
}

export function RichText({ text }: { text: string }) {
  const ideas = allIdeas.value;
  const agents = agentStates.value;
  const out: ComponentChildren[] = [];
  let last = 0;
  let k = 0;
  TOKEN_RE.lastIndex = 0;
  for (let m = TOKEN_RE.exec(text); m; m = TOKEN_RE.exec(text)) {
    const tok = m[0];
    let node: ComponentChildren | null = null;

    if (tok.startsWith("exp/")) {
      node = <ExpLink key={`t${k++}`} label={tok.slice(4)} />;
    } else if (tok.startsWith("idea/")) {
      const id = Number(tok.slice(5));
      if (ideas[id]) node = <IdeaLink key={`t${k++}`} id={id} />;
    } else if (tok.startsWith("#")) {
      const id = Number(tok.slice(1));
      if (ideas[id]) node = <IdeaLink key={`t${k++}`} id={id} />;
    } else if (tok.startsWith("@")) {
      const id = tok.slice(1);
      if (id === "all" || agents[id]) node = <AgentPill key={`t${k++}`} id={id} />;
    } else if (/^\d+\.\d+$/.test(tok)) {
      // Bare "129.5" is an experiment ref only when idea 129 exists — scores
      // like 0.468 stay plain text.
      const ideaId = Number(tok.split(".")[0]);
      if (ideas[ideaId]) node = <ExpLink key={`t${k++}`} label={tok} ideaId={ideaId} />;
    } else if (agents[tok]) {
      // Bare 5-char word that IS a known agent id.
      node = <AgentPill key={`t${k++}`} id={tok} />;
    }

    if (node) {
      if (m.index > last) out.push(text.slice(last, m.index));
      out.push(node);
      last = m.index + tok.length;
    }
  }
  if (last < text.length) out.push(text.slice(last));
  return <>{out}</>;
}
