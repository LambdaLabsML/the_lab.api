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

// Order matters: entity refs first, then ENV vars, hashes, bare numbers.
const TOKEN_RE =
  /(exp\/\d+\.[\w.-]+|idea\/\d+|#\d+|@[a-z0-9]{3,12}\b|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\b\d+\.\d+\b|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,12}\b|\b[a-z][a-z0-9]{4}\b|\b\d+(?:\.\d+)?%?)\b/g;

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
    } else if (/^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$/.test(tok)) {
      // SCREAMING_SNAKE_CASE — almost always an env var / config key.
      node = <code key={`t${k++}`} class="rt-env">{tok}</code>;
    } else if (/^\d+\.\d+$/.test(tok)) {
      // Bare "129.5" is an experiment ref only when idea 129 exists — scores
      // like 0.468 render as data instead.
      const ideaId = Number(tok.split(".")[0]);
      node = ideas[ideaId]
        ? <ExpLink key={`t${k++}`} label={tok} ideaId={ideaId} />
        : <span key={`t${k++}`} class="rt-num">{tok}</span>;
    } else if (/^(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,12}$/.test(tok)) {
      // Hex with both digits and letters at 7–12 chars — a commit-ish hash.
      node = <code key={`t${k++}`} class="rt-env">{tok}</code>;
    } else if (/^[a-z][a-z0-9]{4}$/.test(tok)) {
      // Bare 5-char word that IS a known agent id.
      if (agents[tok]) node = <AgentPill key={`t${k++}`} id={tok} />;
    } else if (/^\d+(?:\.\d+)?%?$/.test(tok)) {
      // Plain numbers read slightly brighter than the (dimmed) prose.
      node = <span key={`t${k++}`} class="rt-num">{tok}</span>;
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
