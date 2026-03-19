import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { getSandboxState, updateSandboxState } from "../state/api";
import type { SandboxCapabilities, SandboxObservedEntry, SandboxState } from "../lib/types";
import { formatTime } from "../lib/format";

function joinRules(lines: string[]): string {
  return lines.join("\n");
}

function splitRules(text: string): string[] {
  return text.split("\n");
}

function snapshot(enabled: boolean, allowText: string, denyText: string): string {
  return JSON.stringify({ enabled, allowText, denyText });
}

export function SandboxView() {
  const [loaded, setLoaded] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [allowText, setAllowText] = useState("");
  const [denyText, setDenyText] = useState("");
  const [builtinAllow, setBuiltinAllow] = useState<string[]>([]);
  const [observed, setObserved] = useState<SandboxObservedEntry[]>([]);
  const [capabilities, setCapabilities] = useState<SandboxCapabilities | null>(null);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [savedSnapshot, setSavedSnapshot] = useState("");

  function applyState(state: SandboxState, overwriteDraft = true) {
    setBuiltinAllow(state.builtin_allowlist || []);
    setObserved(state.observed || []);
    setCapabilities(state.capabilities || null);
    if (overwriteDraft) {
      setEnabled(state.enabled);
      setAllowText(joinRules(state.allowlist || []));
      setDenyText(joinRules(state.denylist || []));
      setSavedSnapshot(snapshot(state.enabled, joinRules(state.allowlist || []), joinRules(state.denylist || [])));
    }
  }

  // Refs so the poll interval always reads current values without re-triggering the effect.
  const enabledRef = useRef(enabled);
  const allowTextRef = useRef(allowText);
  const denyTextRef = useRef(denyText);
  const savedSnapshotRef = useRef(savedSnapshot);
  enabledRef.current = enabled;
  allowTextRef.current = allowText;
  denyTextRef.current = denyText;
  savedSnapshotRef.current = savedSnapshot;

  useEffect(() => {
    let cancelled = false;

    async function loadInitial() {
      try {
        const state = await getSandboxState();
        if (cancelled) return;
        applyState(state, true);
        setLoaded(true);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      }
    }

    loadInitial();

    const interval = window.setInterval(async () => {
      try {
        const state = await getSandboxState();
        if (cancelled) return;
        const dirty = snapshot(enabledRef.current, allowTextRef.current, denyTextRef.current) !== savedSnapshotRef.current;
        applyState(state, !dirty);
      } catch {
        // keep last good state
      }
    }, 5000);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  const currentSnapshot = useMemo(
    () => snapshot(enabled, allowText, denyText),
    [enabled, allowText, denyText],
  );

  useEffect(() => {
    if (!loaded || currentSnapshot === savedSnapshot) return;
    const timer = window.setTimeout(async () => {
      setSaveState("saving");
      setError(null);
      try {
        const state = await updateSandboxState({
          enabled,
          allowlist: splitRules(allowText),
          denylist: splitRules(denyText),
        });
        // Update observed/capabilities from server but keep the user's draft
        // text intact — overwriting it resets cursor position and strips
        // trailing newlines, making continued editing impossible.
        applyState(state, false);
        setSavedSnapshot(currentSnapshot);
        setSaveState("saved");
        window.setTimeout(() => setSaveState((prev) => (prev === "saved" ? "idle" : prev)), 1500);
      } catch (err) {
        setSaveState("error");
        setError(err instanceof Error ? err.message : String(err));
      }
    }, 500);
    return () => window.clearTimeout(timer);
  }, [loaded, currentSnapshot, savedSnapshot, enabled, allowText, denyText]);

  return (
    <div id="sandbox-container">
      <div class="sandbox-header">
        <div>
          <h2>Network Sandbox</h2>
          <p>
            Default-deny outbound network policy for experiments and
            `the-lab-agent` Claude or Codex launches. Built-in package mirrors
            stay allowed; user allow and deny rules apply live to new
            connections.
          </p>
        </div>
        <div class="sandbox-status-wrap">
          <label class="sandbox-toggle">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled((e.target as HTMLInputElement).checked)}
            />
            <span>{enabled ? "Enabled" : "Disabled"}</span>
          </label>
          <div class={`sandbox-save sandbox-save-${saveState}`}>{saveState === "idle" ? "synced" : saveState}</div>
        </div>
      </div>

      {capabilities && !capabilities.available && (
        <div class="sandbox-warning">
          Sandbox runtime unavailable.
          {capabilities.details && <span> {capabilities.details}</span>}
        </div>
      )}
      {error && <div class="sandbox-error">{error}</div>}

      <div class="sandbox-grid">
        <section class="sandbox-panel">
          <div class="sandbox-panel-title">Allowlist</div>
          <div class="sandbox-panel-subtitle">One host or glob per line. Example: `*.example.com`</div>
          <textarea
            value={allowText}
            onInput={(e) => setAllowText((e.target as HTMLTextAreaElement).value)}
            placeholder={"packages.internal.example.com\n*.corp.example"}
          />
        </section>

        <section class="sandbox-panel">
          <div class="sandbox-panel-title">Denylist</div>
          <div class="sandbox-panel-subtitle">Deny rules win over built-in and user allow rules.</div>
          <textarea
            value={denyText}
            onInput={(e) => setDenyText((e.target as HTMLTextAreaElement).value)}
            placeholder={"github.com\n*.social.example"}
          />
        </section>
      </div>

      <section class="sandbox-panel builtin-panel">
        <div class="sandbox-panel-title">Built-in Installer Allowlist</div>
        <div class="sandbox-panel-subtitle">Derived from pip/uv configuration and apt sources.</div>
        <div class="sandbox-chip-list">
          {builtinAllow.map((rule) => (
            <span class="sandbox-chip" key={rule}>{rule}</span>
          ))}
          {builtinAllow.length === 0 && <span class="sandbox-empty">No installer hosts detected.</span>}
        </div>
      </section>

      <section class="sandbox-panel observed-panel">
        <div class="sandbox-panel-title">Observed Accesses</div>
        <div class="sandbox-panel-subtitle">Gray list of requested destinations, aggregated from the live access log.</div>
        <div class="sandbox-table-wrap">
          <table class="sandbox-table">
            <thead>
              <tr>
                <th>Host</th>
                <th>Kind</th>
                <th>Attempts</th>
                <th>Allowed</th>
                <th>Blocked</th>
                <th>Last Seen</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {observed.map((row) => (
                <tr key={`${row.host}:${row.port}:${row.kind}`}>
                  <td>
                    <div class="sandbox-host">{row.host}</div>
                    <div class="sandbox-meta">
                      port {row.port}
                      {row.ips.length > 0 && ` · ${row.ips.join(", ")}`}
                    </div>
                  </td>
                  <td>
                    <div>{row.kind}</div>
                    {row.labels.length > 0 && <div class="sandbox-meta">{row.labels.join(", ")}</div>}
                  </td>
                  <td>{row.attempts}</td>
                  <td>{row.allowed}</td>
                  <td>{row.blocked}</td>
                  <td>{formatTime(row.last_seen || "")}</td>
                  <td>{row.top_reason}</td>
                </tr>
              ))}
              {observed.length === 0 && (
                <tr>
                  <td colSpan={7} class="sandbox-empty-cell">No accesses logged yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
