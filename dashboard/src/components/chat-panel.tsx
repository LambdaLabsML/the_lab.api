import { useState, useEffect, useLayoutEffect, useRef } from "preact/hooks";
import { getChatStatus, streamChat } from "../state/api";

interface Message {
  role: "user" | "assistant";
  content: string;
}

const STORAGE_KEY = "the-lab-chat-position";
const FAB_SIZE = 44;
const CLAMP_MARGIN = 10;

function getPanelSize(): { width: number; height: number } {
  if (typeof window === "undefined") return { width: 420, height: 540 };
  const w = window.innerWidth;
  if (w <= 500) {
    return {
      width: Math.max(0, w - 20),
      height: Math.max(0, window.innerHeight - 80),
    };
  }
  return { width: 420, height: 540 };
}

function getDefaultMargins(): { x: number; y: number } {
  return window.innerWidth <= 500 ? { x: 10, y: 10 } : { x: 20, y: 20 };
}

function defaultPosition(): { left: number; top: number } {
  const { width, height } = getPanelSize();
  const m = getDefaultMargins();
  return clamp(
    window.innerWidth - width - m.x,
    window.innerHeight - height - m.y,
    width,
    height,
  );
}

function clamp(
  left: number,
  top: number,
  width: number,
  height: number,
): { left: number; top: number } {
  const m = CLAMP_MARGIN;
  const maxW = window.innerWidth;
  const maxH = window.innerHeight;
  return {
    left: Math.min(Math.max(m, left), maxW - width - m),
    top: Math.min(Math.max(m, top), maxH - height - m),
  };
}

function loadPosition(): { left: number; top: number } | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as unknown;
    if (
      p &&
      typeof p === "object" &&
      "left" in p &&
      "top" in p &&
      typeof (p as { left: unknown }).left === "number" &&
      typeof (p as { top: unknown }).top === "number"
    ) {
      return { left: (p as { left: number }).left, top: (p as { top: number }).top };
    }
  } catch {
    /* ignore */
  }
  return null;
}

function savePosition(p: { left: number; top: number }) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(p));
  } catch {
    /* ignore */
  }
}

export function ChatPanel() {
  const [available, setAvailable] = useState<boolean | null>(null);
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const dragRef = useRef<{ dx: number; dy: number } | null>(null);

  useEffect(() => {
    getChatStatus()
      .then((s) => setAvailable(s.available))
      .catch(() => setAvailable(false));
  }, []);

  useLayoutEffect(() => {
    if (available !== true) return;
    const saved = loadPosition();
    const { width, height } = getPanelSize();
    if (saved) {
      setPos(clamp(saved.left, saved.top, width, height));
    } else {
      setPos(defaultPosition());
    }
  }, [available]);

  useEffect(() => {
    if (available !== true) return;
    const onResize = () => {
      setPos((p) => {
        if (!p) return p;
        const { width, height } = getPanelSize();
        return clamp(p.left, p.top, width, height);
      });
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [available]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open]);

  if (available === null || available === false || pos === null) return null;

  const position = pos;
  const { width: panelW, height: panelH } = getPanelSize();
  const fabLeft = position.left + panelW - FAB_SIZE;
  const fabTop = position.top + panelH - FAB_SIZE;

  function handleHeaderPointerDown(e: PointerEvent) {
    if ((e.target as HTMLElement).closest("button")) return;
    dragRef.current = { dx: e.clientX - position.left, dy: e.clientY - position.top };
    setDragging(true);
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }

  function handleHeaderPointerMove(e: PointerEvent) {
    if (!dragRef.current) return;
    const { width, height } = getPanelSize();
    setPos(
      clamp(
        e.clientX - dragRef.current.dx,
        e.clientY - dragRef.current.dy,
        width,
        height,
      ),
    );
  }

  function handleHeaderPointerUp(e: PointerEvent) {
    const drag = dragRef.current;
    if (drag) {
      const { width, height } = getPanelSize();
      const next = clamp(
        e.clientX - drag.dx,
        e.clientY - drag.dy,
        width,
        height,
      );
      setPos(next);
      savePosition(next);
    }
    dragRef.current = null;
    setDragging(false);
    (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
  }

  function handleSend() {
    const text = input.trim();
    if (!text || streaming) return;
    setInput("");

    const userMsg: Message = { role: "user", content: text };
    const allMessages = [...messages, userMsg];
    setMessages([...allMessages, { role: "assistant", content: "" }]);
    setStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    const assistantIdx = allMessages.length;

    streamChat(
      allMessages.map((m) => ({ role: m.role, content: m.content })),
      (chunk) => {
        setMessages((prev) => {
          const updated = [...prev];
          updated[assistantIdx] = {
            ...updated[assistantIdx],
            content: updated[assistantIdx].content + chunk,
          };
          return updated;
        });
      },
      () => {
        setStreaming(false);
        abortRef.current = null;
      },
      (err) => {
        setMessages((prev) => {
          const updated = [...prev];
          updated[assistantIdx] = {
            ...updated[assistantIdx],
            content: updated[assistantIdx].content || `Error: ${err}`,
          };
          return updated;
        });
        setStreaming(false);
        abortRef.current = null;
      },
      controller.signal,
    );
  }

  function handleStop() {
    abortRef.current?.abort();
    setStreaming(false);
    abortRef.current = null;
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function handleClear() {
    if (!streaming) {
      setMessages([]);
    }
  }

  return (
    <>
      {!open && (
        <button
          id="chat-fab"
          type="button"
          style={{ left: fabLeft, top: fabTop }}
          onClick={() => setOpen(true)}
          title="Ask about research"
        >
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
        </button>
      )}

      {open && (
        <div id="chat-panel" style={{ left: position.left, top: position.top }}>
          <div
            class={`chat-header${dragging ? " chat-header-dragging" : ""}`}
            onPointerDown={handleHeaderPointerDown}
            onPointerMove={handleHeaderPointerMove}
            onPointerUp={handleHeaderPointerUp}
            onPointerCancel={handleHeaderPointerUp}
          >
            <span class="chat-title">Research Chat</span>
            <div class="chat-header-actions">
              {messages.length > 0 && !streaming && (
                <button class="chat-clear-btn" onClick={handleClear} title="Clear conversation">
                  Clear
                </button>
              )}
              <button class="chat-close-btn" onClick={() => setOpen(false)} title="Close">
                &times;
              </button>
            </div>
          </div>

          <div class="chat-messages">
            {messages.length === 0 && (
              <div class="chat-empty">
                Ask anything about your research project — experiment comparisons,
                best results, key insights, or trends.
              </div>
            )}
            {messages.map((msg, i) => (
              <div key={i} class={`chat-msg chat-msg-${msg.role}`}>
                <div class="chat-msg-label">{msg.role === "user" ? "You" : "Assistant"}</div>
                <div class="chat-msg-content">
                  {msg.content || (streaming && i === messages.length - 1 ? "..." : "")}
                </div>
              </div>
            ))}
            <div ref={messagesEndRef} />
          </div>

          <div class="chat-input-area">
            <textarea
              ref={inputRef}
              class="chat-input"
              value={input}
              onInput={(e) => setInput((e.target as HTMLTextAreaElement).value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask a question..."
              rows={2}
              disabled={streaming}
            />
            {streaming ? (
              <button class="chat-send-btn chat-stop-btn" onClick={handleStop} title="Stop">
                Stop
              </button>
            ) : (
              <button
                class="chat-send-btn"
                onClick={handleSend}
                disabled={!input.trim()}
                title="Send (Enter)"
              >
                Send
              </button>
            )}
          </div>
        </div>
      )}
    </>
  );
}
