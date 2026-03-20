import { useState, useEffect, useRef } from "preact/hooks";
import { getChatStatus, streamChat } from "../state/api";

interface Message {
  role: "user" | "assistant";
  content: string;
}

export function ChatPanel() {
  const [available, setAvailable] = useState<boolean | null>(null);
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    getChatStatus()
      .then((s) => setAvailable(s.available))
      .catch(() => setAvailable(false));
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open]);

  if (available === null || available === false) return null;

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
        <button id="chat-fab" onClick={() => setOpen(true)} title="Ask about research">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
        </button>
      )}

      {open && (
        <div id="chat-panel">
          <div class="chat-header">
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
