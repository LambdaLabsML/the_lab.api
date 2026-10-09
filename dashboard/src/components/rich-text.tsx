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
    } else if (/^\d+\.\d+$/.test(value) && parseInt(value, 10) >= 100 && hasExperiment(value)) {
      // Bare "N.M" is read as an experiment only for idea N >= 100: small
      // decimals are almost always quantities ("20.2 s", tables of numbers).
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

// ── Block markdown (messages): lists, tables, headings, code, quotes ───────
// Built from Preact nodes like the inline pass, so agent text never becomes
// HTML. Lines that are none of these stay as lines (messages rely on single
// newlines), so plain-text messages render exactly as before.

const FENCE = /^\s*```/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function splitRow(line: string): string[] {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
  return row.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

interface ListNode { ordered: boolean; start: number; items: { text: string[]; children: ListNode[] }[]; indent: number }

function listNodes(list: ListNode, key: { value: number }): ComponentChildren {
  const items = list.items.map((item) => (
    <li key={`rt-${key.value++}`}>
      {item.text.map((t, i) => <span key={`rt-${key.value++}`}>{i > 0 && <br />}{inlineNodes(t, key)}</span>)}
      {item.children.map((child) => listNodes(child, key))}
    </li>
  ));
  return list.ordered
    ? <ol key={`rt-${key.value++}`} class="rt-list" start={list.start !== 1 ? list.start : undefined}>{items}</ol>
    : <ul key={`rt-${key.value++}`} class="rt-list">{items}</ul>;
}

function parseList(lines: string[], start: number): { node: ListNode; next: number } {
  const first = LIST_ITEM.exec(lines[start])!;
  const root: ListNode = { ordered: /\d/.test(first[2]), start: parseInt(first[2], 10) || 1, items: [], indent: first[1].length };
  const stack: ListNode[] = [root];
  let i = start;
  for (; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) {
      // A blank line ends the list unless the next line continues it.
      const nextLine = lines[i + 1];
      if (nextLine !== undefined && (LIST_ITEM.test(nextLine) || /^\s{2,}\S/.test(nextLine))) continue;
      break;
    }
    const m = LIST_ITEM.exec(line);
    if (m) {
      const indent = m[1].length;
      while (stack.length > 1 && indent < stack[stack.length - 1].indent) stack.pop();
      let top = stack[stack.length - 1];
      if (indent > top.indent && top.items.length) {
        const child: ListNode = { ordered: /\d/.test(m[2]), start: parseInt(m[2], 10) || 1, items: [], indent };
        top.items[top.items.length - 1].children.push(child);
        stack.push(child);
        top = child;
      }
      top.items.push({ text: [m[3]], children: [] });
      continue;
    }
    // Indented continuation of the current item; anything else ends the list.
    if (/^\s+\S/.test(line) && stack[stack.length - 1].items.length) {
      const top = stack[stack.length - 1];
      top.items[top.items.length - 1].text.push(line.trim());
      continue;
    }
    break;
  }
  return { node: root, next: i };
}

function blockNodes(text: string, key: { value: number }): ComponentChildren[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const out: ComponentChildren[] = [];
  let para: string[] = [];
  const flush = () => {
    if (!para.length) return;
    out.push(
      <p key={`rt-${key.value++}`} class="rt-p">
        {para.map((line, i) => <span key={`rt-${key.value++}`} class="rt-line">{inlineNodes(line, key)}{i < para.length - 1 && <br />}</span>)}
      </p>,
    );
    para = [];
  };
  for (let i = 0; i < lines.length; ) {
    const line = lines[i];
    if (FENCE.test(line)) {
      flush();
      const body: string[] = [];
      for (i += 1; i < lines.length && !FENCE.test(lines[i]); i++) body.push(lines[i]);
      i += 1;
      out.push(<pre key={`rt-${key.value++}`} class="rt-pre"><code>{body.join("\n")}</code></pre>);
      continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    const h = HEADING.exec(line);
    if (h) {
      flush();
      out.push(<div key={`rt-${key.value++}`} class={`rt-h rt-h${h[1].length}`}>{inlineNodes(h[2], key)}</div>);
      i++;
      continue;
    }
    if (RULE.test(line)) {
      flush(); out.push(<hr key={`rt-${key.value++}`} class="rt-hr" />); i++; continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      flush();
      const head = splitRow(line);
      const align = splitRow(lines[i + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : undefined));
      const rows: string[][] = [];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i++) rows.push(splitRow(lines[i]));
      out.push(
        <div key={`rt-${key.value++}`} class="rt-table-wrap">
          <table class="rt-table">
            <thead><tr>{head.map((c, j) => <th key={j} style={align[j] ? { textAlign: align[j] } : undefined}>{inlineNodes(c, key)}</th>)}</tr></thead>
            <tbody>{rows.map((r, ri) => (
              <tr key={ri}>{head.map((_, j) => <td key={j} style={align[j] ? { textAlign: align[j] } : undefined}>{inlineNodes(r[j] ?? "", key)}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (QUOTE.test(line)) {
      flush();
      const body: string[] = [];
      for (; i < lines.length && QUOTE.test(lines[i]); i++) body.push(QUOTE.exec(lines[i])![1]);
      out.push(<blockquote key={`rt-${key.value++}`} class="rt-quote">{blockNodes(body.join("\n"), key)}</blockquote>);
      continue;
    }
    if (LIST_ITEM.test(line)) {
      flush();
      const { node, next } = parseList(lines, i);
      out.push(listNodes(node, key));
      i = next;
      continue;
    }
    para.push(line);
    i++;
  }
  flush();
  return out;
}

/** Message text. ``inline``: one flowing span (compact rows such as the agent
 *  tree); otherwise block markdown — lists, tables, headings, code blocks,
 *  quotes — with the same inline links and entity references. */
export function RichText({ text, inline = false }: { text: string; inline?: boolean }) {
  const key = { value: 0 };
  if (inline) {
    const lines = text.replace(/\r\n?/g, "\n").split("\n");
    return <span class="rich-text">{lines.map((line, index) => <span key={`line-${index}`} class="rt-line">{inlineNodes(line, key)}{index < lines.length - 1 && <br />}</span>)}</span>;
  }
  return <div class="rich-text rich-text--blocks">{blockNodes(text, key)}</div>;
}
