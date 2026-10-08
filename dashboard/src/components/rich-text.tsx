/** Safe, small Markdown renderer for messages. It always creates Preact nodes
 * and never assigns message text to innerHTML. */
import type { ComponentChildren } from "preact";
import { ExpLink } from "./exp-link";
import { IdeaLink } from "./idea-link";
import { AgentPill } from "./agent-pill";
import { allExperiments, allIdeas } from "../state/signals";
import { agentStates, focusMessageId, showMessagePreviewById, hideMessagePreview } from "../state/agent-activity";
import { latestMessageId } from "../state/messages";
import { agentColor } from "../lib/colors";

export function stripRouting(text: string): string {
  return text.replace(/^\s*\[[^\]]{0,60}\]\s*/, "");
}

function safeHref(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch { return null; }
}

function trimUrlPunctuation(value: string): [string, string] {
  let end = value.length;
  while (end > 0 && /[.,!?;:]/.test(value[end - 1])) end--;
  while (end > 0 && value[end - 1] === ")") {
    const part = value.slice(0, end);
    if ((part.match(/\(/g) ?? []).length >= (part.match(/\)/g) ?? []).length) break;
    end--;
  }
  return [value.slice(0, end), value.slice(end)];
}

/** A message reference ("msg 5510", "thread 4123", or a bare id): hover shows
 *  the message, click opens it in its thread in Messages. */
function MsgRef({ id, bare = false, children }: { id: number; bare?: boolean; children: ComponentChildren }) {
  return (
    <a
      class={`rt-msg${bare ? " rt-msg-bare" : ""}`}
      href="#messages"
      onMouseEnter={(e) => showMessagePreviewById(id, e.currentTarget as Element)}
      onMouseLeave={hideMessagePreview}
      onClick={(e) => {
        e.preventDefault();
        hideMessagePreview();
        focusMessageId.value = id;
        window.location.hash = "#messages";
      }}
    >
      {children}
    </a>
  );
}

// "msg 5510", "msgs 4165/4171", "messages 5843, 5847 and 5851", "thread 4123"
// (a thread's id is its first message's id).
const MSG_REF = /^(msgs?|messages?|thread)(\s*#?)(\d+(?:\s*(?:\/|,|&|and)\s*#?\d+)*)$/i;

function msgRefNodes(value: string, key: { value: number }): ComponentChildren[] {
  const m = MSG_REF.exec(value)!;
  const out: ComponentChildren[] = [m[1] + m[2]];
  const parts = m[3].split(/(\d+)/);
  for (const part of parts) {
    if (/^\d+$/.test(part)) out.push(<MsgRef key={`rt-${key.value++}`} id={Number(part)}>{part}</MsgRef>);
    else if (part) out.push(part);
  }
  return out;
}

function knownAgent(id: string, agents: Record<string, unknown>): string | null {
  const lower = id.toLowerCase();
  if (lower === "all") return "all";
  return Object.keys(agents).find((name) => name.toLowerCase() === lower) ?? null;
}

function entityNodes(text: string, key: { value: number }): ComponentChildren[] {
  const ideas = allIdeas.value;
  const experiments = allExperiments.value;
  const agents = agentStates.value as Record<string, unknown>;
  const newestMsg = latestMessageId.value;
  const token = /((?:msgs?|messages?|thread)\s*#?\d+(?:\s*(?:\/|,|&|and)\s*#?\d+)*\b|exp\/\d+\.\d+|idea\/\d+|#\d+|@[a-z][a-z0-9_-]*\b|\b\d+\.\d+\b|\b\d+\b|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,12}\b)/gi;
  const out: ComponentChildren[] = [];
  let last = 0;
  const hasExperiment = (label: string) => experiments.some((exp) => String(exp.label ?? exp.id) === label);

  for (let match = token.exec(text); match; match = token.exec(text)) {
    const value = match[0], lower = value.toLowerCase();
    let node: ComponentChildren | null = null;
    if (MSG_REF.test(value)) {
      if (match.index > last) out.push(text.slice(last, match.index));
      out.push(...msgRefNodes(value, key));
      last = match.index + value.length;
      continue;
    } else if (lower.startsWith("exp/")) {
      const label = value.slice(4);
      if (hasExperiment(label)) node = <ExpLink key={`rt-${key.value++}`} label={label} />;
    } else if (lower.startsWith("idea/")) {
      const id = Number(value.slice(5));
      if (ideas[id]) node = <IdeaLink key={`rt-${key.value++}`} id={id} />;
    } else if (value.startsWith("#")) {
      const id = Number(value.slice(1));
      if (ideas[id]) node = <IdeaLink key={`rt-${key.value++}`} id={id} />;
    } else if (value.startsWith("@")) {
      const agent = knownAgent(value.slice(1), agents);
      node = agent ? <span key={`rt-${key.value++}`} class="rt-mention rt-mention-known" style={{ color: agentColor(agent) }}><AgentPill id={agent} /></span>
        : <span key={`rt-${key.value++}`} class="rt-mention">{value}</span>;
    } else if (/^\d+\.\d+$/.test(value) && hasExperiment(value)) {
      node = <ExpLink key={`rt-${key.value++}`} label={value} />;
    } else if (/^\d+$/.test(value)) {
      const id = Number(value);
      // Ideas are written #N or idea/N, so a bare number is never an idea. In
      // the message id range it may be a bare message ref ("→ David 5843:",
      // "(5843/5847)"): hover to check, click to open.
      node = id >= 100 && id <= newestMsg ? <MsgRef key={`rt-${key.value++}`} id={id} bare><span class="rt-num">{value}</span></MsgRef>
        : <span key={`rt-${key.value++}`} class="rt-num">{value}</span>;
    } else if (/^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$/.test(value) || /^(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,12}$/i.test(value)) {
      node = <code key={`rt-${key.value++}`} class="rt-env">{value}</code>;
    }
    if (node) {
      if (match.index > last) out.push(text.slice(last, match.index));
      out.push(node);
      last = match.index + value.length;
    }
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function plainNodes(source: string, key: { value: number }): ComponentChildren[] {
  const out: ComponentChildren[] = [], urls = /https?:\/\/[^\s<>{}\[\]]+/gi;
  let last = 0;
  for (let match = urls.exec(source); match; match = urls.exec(source)) {
    if (match.index > last) out.push(...entityNodes(source.slice(last, match.index), key));
    const [url, trailing] = trimUrlPunctuation(match[0]);
    const href = safeHref(url);
    out.push(href ? <a key={`rt-${key.value++}`} class="rt-link" href={href} target="_blank" rel="noopener noreferrer">{url}</a> : url);
    if (trailing) out.push(trailing);
    last = match.index + match[0].length;
  }
  if (last < source.length) out.push(...entityNodes(source.slice(last), key));
  return out;
}

function inlineNodes(source: string, key: { value: number }): ComponentChildren[] {
  const out: ComponentChildren[] = [];
  const markdown = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|`([^`\n]+)`|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*/g;
  let last = 0;
  for (let match = markdown.exec(source); match; match = markdown.exec(source)) {
    if (match.index > last) out.push(...plainNodes(source.slice(last, match.index), key));
    if (match[1] != null) {
      const href = safeHref(match[2]);
      out.push(href ? <a key={`rt-${key.value++}`} class="rt-link" href={href} target="_blank" rel="noopener noreferrer">{inlineNodes(match[1], key)}</a> : match[0]);
    } else if (match[3] != null) out.push(<code key={`rt-${key.value++}`} class="rt-code">{match[3]}</code>);
    else if (match[4] != null) out.push(<strong key={`rt-${key.value++}`}>{inlineNodes(match[4], key)}</strong>);
    else if (match[5] != null) out.push(<em key={`rt-${key.value++}`}>{inlineNodes(match[5], key)}</em>);
    last = match.index + match[0].length;
  }
  if (last < source.length) out.push(...plainNodes(source.slice(last), key));
  return out;
}

export function RichText({ text }: { text: string }) {
  const key = { value: 0 }, lines = text.replace(/\r\n?/g, "\n").split("\n");
  return <span class="rich-text">{lines.map((line, index) => <span key={`line-${index}`} class="rt-line">{inlineNodes(line, key)}{index < lines.length - 1 && <br />}</span>)}</span>;
}
