/**
 * Message FAB — floating button that opens a quick message composer.
 * Sends messages to agents via POST /api/v1/messages and shows recent history.
 * Replaces the old "Research Chat" LLM panel.
 */
import { useState, useEffect, useRef } from "preact/hooks";
import { listMessages, sendMessage } from "../state/api";
import type { MessageEntry } from "../lib/types";

const FAB_SIZE = 44;
const CLAMP_MARGIN = 10;

function clamp(left: number, top: number): { left: number; top: number } {
  const panelW = window.innerWidth <= 500 ? Math.max(0, window.innerWidth - 20) : 380;
  const panelH = 420;
  return {
    left: Math.min(Math.max(CLAMP_MARGIN, left), window.innerWidth - panelW - CLAMP_MARGIN),
    top: Math.min(Math.max(CLAMP_MARGIN, top), window.innerHeight - panelH - CLAMP_MARGIN),
  };
}

function relTime(iso: string): string {
  const sec = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  return `${Math.floor(min / 60)}h ago`;
}

export function ChatPanel() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<MessageEntry[]>([]);
  const [input, setInput] = useState("");
  const [to, setTo] = useState("all");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{ dx: number; dy: number } | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Default position: bottom-right corner
  useEffect(() => {
    const panelW = window.innerWidth <= 500 ? window.innerWidth - 20 : 380;
    const panelH = 420;
    const m = window.innerWidth <= 500 ? 10 : 20;
    setPos(clamp(window.innerWidth - panelW - m, window.innerHeight - panelH - m));
  }, []);

  // Resize handler
  useEffect(() => {
    const onResize = () => setPos((p) => p ? clamp(p.left, p.top) : p);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Poll messages when open
  useEffect(() => {
    if (!open) return;
    const load = () => listMessages(30).then(setMessages).catch(() => {});
    load();
    const t = window.setInterval(load, 5000);
    return () => clearInterval(t);
  }, [open]);

  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  if (pos === null) return null;

  const fabLeft = pos.left + (window.innerWidth <= 500 ? window.innerWidth - 20 : 380) - FAB_SIZE;
  const fabTop  = pos.top  + 420 - FAB_SIZE;

  function handlePointerDown(e: PointerEvent) {
    if ((e.target as HTMLElement).closest("button,textarea,select")) return;
    dragRef.current = { dx: e.clientX - pos!.left, dy: e.clientY - pos!.top };
    setDragging(true);
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }
  function handlePointerMove(e: PointerEvent) {
    if (!dragRef.current) return;
    setPos(clamp(e.clientX - dragRef.current.dx, e.clientY - dragRef.current.dy));
  }
  function handlePointerUp() {
    dragRef.current = null;
    setDragging(false);
  }

  async function handleSend() {
    const text = input.trim();
    if (!text || sending) return;
    setSending(true);
    setError(null);
    try {
      await sendMessage(to, text);
      setInput("");
      const updated = await listMessages(30);
      setMessages(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to send");
    } finally {
      setSending(false);
    }
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
  }

  const panelW = window.innerWidth <= 500 ? Math.max(0, window.innerWidth - 20) : 380;

  return (
    <>
      {!open && (
        <button
          id="chat-fab"
          type="button"
          style={{ left: fabLeft, top: fabTop }}
          onClick={() => setOpen(true)}
          title="Message agents"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 2L11 13" /><path d="M22 2L15 22l-4-9-9-4 20-7z" />
          </svg>
        </button>
      )}

      {open && (
        <div
          id="chat-panel"
          style={{ left: pos.left, top: pos.top, width: panelW }}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerCancel={handlePointerUp}
        >
          <div class={`chat-header${dragging ? " chat-header-dragging" : ""}`}>
            <span class="chat-title">Message agents</span>
            <button class="chat-close-btn" onClick={() => setOpen(false)} title="Close">×</button>
          </div>

          {/* Recent messages */}
          <div class="chat-messages" ref={listRef}>
            {messages.length === 0 ? (
              <div class="chat-empty">
                Messages sent here are delivered to all running agents via
                <code> _notifications</code>. Use this to give instructions mid-run.
              </div>
            ) : (
              [...messages].reverse().map((m) => (
                <div key={m.id} class={`chat-msg chat-msg-${m.from_agent ? "agent" : "user"}`}>
                  <div class="chat-msg-label">
                    {m.from_agent
                      ? (m.from_role ? `${m.from_agent} (${m.from_role})` : m.from_agent)
                      : "you"}
                    <span class="chat-msg-time">{m.created_at ? relTime(m.created_at) : ""}</span>
                  </div>
                  <div class="chat-msg-content">{m.text}</div>
                </div>
              ))
            )}
          </div>

          {error && <div class="chat-error">{error}</div>}

          <div class="chat-input-area">
            <select
              class="chat-to-select"
              value={to}
              onChange={(e) => setTo((e.target as HTMLSelectElement).value)}
            >
              <option value="all">→ all agents</option>
              <option value="role:default">→ role: default</option>
            </select>
            <div class="chat-input-row">
              <textarea
                ref={inputRef}
                class="chat-input"
                value={input}
                onInput={(e) => setInput((e.target as HTMLTextAreaElement).value)}
                onKeyDown={handleKeyDown}
                placeholder="Message agents… (Enter to send)"
                rows={2}
                disabled={sending}
              />
              <button
                type="button"
                class={`ui-btn chat-send-btn${input.trim() && !sending ? " is-active" : ""}`}
                onClick={handleSend}
                disabled={!input.trim() || sending}
              >
                {sending ? "…" : "Send"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
