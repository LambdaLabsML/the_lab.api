/**
 * MessagePreviewOverlay — hovering a message row in the sidebar shows the full
 * message as a floating card over the main area (fade-in, lives for the
 * duration of the hover). Mounted once in the app shell.
 */
import { messagePreview } from "../../state/agent-activity";
import { AgentPill } from "../agent-pill";
import { MsgIcon } from "./icons";
import { RichText, stripRouting } from "../rich-text";

function clock(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function MessagePreviewOverlay() {
  const p = messagePreview.value;
  if (!p) return null;
  return (
    <div class="msg-preview" role="presentation">
      <div class="msg-preview-head">
        {p.from && <AgentPill id={p.from} />}
        <span class="msg-preview-icon"><MsgIcon /></span>
        <AgentPill id={p.to} />
        <span class="msg-preview-time">{clock(p.ts)}</span>
      </div>
      <div class="msg-preview-body">{p.body ? <RichText text={stripRouting(p.body)} /> : "…"}</div>
      <div class="msg-preview-hint">
        {p.full ? "click to open in Messages" : "loading full text…"}
      </div>
    </div>
  );
}
