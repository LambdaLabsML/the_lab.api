/** Safe, small Markdown renderer for messages. It always creates Preact nodes
 * and never assigns message text to innerHTML. */
import type { ComponentChildren } from "preact";
import { ExpLink } from "./exp-link";
import { IdeaLink } from "./idea-link";
import { AgentPill } from "./agent-pill";
import { allExperiments, allIdeas } from "../state/signals";
import { agentStates } from "../state/agent-activity";
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

function knownAgent(id: string, agents: Record<string, unknown>): string | null {
  const lower = id.toLowerCase();
  if (lower === "all") return "all";
  return Object.keys(agents).find((name) => name.toLowerCase() === lower) ?? null;
}

function entityNodes(text: string, key: { value: number }): ComponentChildren[] {
  const ideas = allIdeas.value;
  const experiments = allExperiments.value;
  const agents = agentStates.value as Record<string, unknown>;
  const token = /(exp\/\d+\.\d+|idea\/\d+|#\d+|@[a-z][a-z0-9_-]*\b|\b\d+\.\d+\b|\b\d+\b|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,12}\b)/gi;
  const out: ComponentChildren[] = [];
  let last = 0;
  const hasExperiment = (label: string) => experiments.some((exp) => String(exp.label ?? exp.id) === label);

  for (let match = token.exec(text); match; match = token.exec(text)) {
    const value = match[0], lower = value.toLowerCase();
    let node: ComponentChildren | null = null;
    if (lower.startsWith("exp/")) {
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
      node = ideas[id] ? <IdeaLink key={`rt-${key.value++}`} id={id} />
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
