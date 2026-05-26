import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { getSandboxState, updateSandboxState } from "../state/api";
import type { SandboxCapabilities, SandboxFileBind, SandboxObservedEntry, SandboxState } from "../lib/types";
import { formatTime } from "../lib/format";

interface ToggleModalProps {
  enabling: boolean;
  hasPassword: boolean;
  onConfirm: (password: string) => void;
  onCancel: () => void;
  error: string | null;
  busy: boolean;
}

function ToggleModal({ enabling, hasPassword, onConfirm, onCancel, error, busy }: ToggleModalProps) {
  const [pw, setPw] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  function handleSubmit(e: Event) {
    e.preventDefault();
    onConfirm(pw);
  }

  return (
    <div class="sandbox-modal-backdrop" onClick={onCancel}>
      <div class="sandbox-modal" onClick={(e) => e.stopPropagation()}>
        <h3>{enabling ? "Enable Sandbox" : "Disable Sandbox"}</h3>
        <form onSubmit={handleSubmit}>
          {enabling ? (
            <>
              <p>Set a password required to disable the sandbox later.<br />This prevents agents from turning it off.</p>
              <input
                ref={inputRef}
                type="password"
                placeholder="Disable password"
                value={pw}
                onInput={(e) => setPw((e.target as HTMLInputElement).value)}
                required
                minLength={1}
              />
            </>
          ) : (
            <>
              <p>Enter the disable password to turn off the sandbox.</p>
              <input
                ref={inputRef}
                type="password"
                placeholder="Disable password"
                value={pw}
                onInput={(e) => setPw((e.target as HTMLInputElement).value)}
                required={hasPassword}
              />
            </>
          )}
          {error && <div class="sandbox-modal-error">{error}</div>}
          <div class="sandbox-modal-actions">
            <button type="button" class="sandbox-modal-cancel" onClick={onCancel} disabled={busy}>Cancel</button>
            <button type="submit" class="sandbox-modal-confirm" disabled={busy || (enabling && !pw)}>
              {busy ? "…" : enabling ? "Enable" : "Disable"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function joinRules(lines: string[]): string {
  return lines.join("\n");
}

function splitRules(text: string): string[] {
  return text.split("\n");
}

function snapshot(
  enabled: boolean,
  allowText: string,
  denyText: string,
  rwText: string,
  roText: string,
): string {
  return JSON.stringify({ enabled, allowText, denyText, rwText, roText });
}

export function SandboxView() {
  const [loaded, setLoaded] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [hasDisablePassword, setHasDisablePassword] = useState(false);
  const [allowText, setAllowText] = useState("");
  const [denyText, setDenyText] = useState("");
  const [rwText, setRwText] = useState("");
  const [roText, setRoText] = useState("");
  const [builtinAllow, setBuiltinAllow] = useState<string[]>([]);
  const [builtinRw, setBuiltinRw] = useState<string[]>([]);
  const [builtinRo, setBuiltinRo] = useState<string[]>([]);
  const [builtinFileBinds, setBuiltinFileBinds] = useState<SandboxFileBind[]>([]);
  const [observed, setObserved] = useState<SandboxObservedEntry[]>([]);
  const [capabilities, setCapabilities] = useState<SandboxCapabilities | null>(null);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [savedSnapshot, setSavedSnapshot] = useState("");
  // Modal state for the enable/disable toggle
  const [modal, setModal] = useState<{ enabling: boolean } | null>(null);
  const [modalError, setModalError] = useState<string | null>(null);
  const [modalBusy, setModalBusy] = useState(false);

  function applyState(state: SandboxState, overwriteDraft = true) {
    setBuiltinAllow(state.builtin_allowlist || []);
    setBuiltinRw(state.builtin_file_rw || []);
    setBuiltinRo(state.builtin_file_ro || []);
    setBuiltinFileBinds(state.builtin_file_binds || []);
    setObserved(state.observed || []);
    setCapabilities(state.capabilities || null);
    setHasDisablePassword(state.has_disable_password || false);
    if (overwriteDraft) {
      setEnabled(state.enabled);
      setAllowText(joinRules(state.allowlist || []));
      setDenyText(joinRules(state.denylist || []));
      setRwText(joinRules(state.file_rw || []));
      setRoText(joinRules(state.file_ro || []));
      setSavedSnapshot(snapshot(
        state.enabled,
        joinRules(state.allowlist || []),
        joinRules(state.denylist || []),
        joinRules(state.file_rw || []),
        joinRules(state.file_ro || []),
      ));
    }
  }

  const enabledRef = useRef(enabled);
  const allowTextRef = useRef(allowText);
  const denyTextRef = useRef(denyText);
  const rwTextRef = useRef(rwText);
  const roTextRef = useRef(roText);
  const savedSnapshotRef = useRef(savedSnapshot);
  enabledRef.current = enabled;
  allowTextRef.current = allowText;
  denyTextRef.current = denyText;
  rwTextRef.current = rwText;
  roTextRef.current = roText;
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
        const dirty = snapshot(
          enabledRef.current,
          allowTextRef.current,
          denyTextRef.current,
          rwTextRef.current,
          roTextRef.current,
        ) !== savedSnapshotRef.current;
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
    () => snapshot(enabled, allowText, denyText, rwText, roText),
    [enabled, allowText, denyText, rwText, roText],
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
          file_rw: splitRules(rwText),
          file_ro: splitRules(roText),
        });
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
  }, [loaded, currentSnapshot, savedSnapshot, enabled, allowText, denyText, rwText, roText]);

  async function handleModalConfirm(password: string) {
    if (!modal) return;
    setModalBusy(true);
    setModalError(null);
    try {
      const state = await updateSandboxState({
        enabled: modal.enabling,
        allowlist: splitRules(allowText),
        denylist: splitRules(denyText),
        file_rw: splitRules(rwText),
        file_ro: splitRules(roText),
        disable_password: password || undefined,
      });
      applyState(state, false);
      setEnabled(modal.enabling);
      setSavedSnapshot(snapshot(
        modal.enabling, allowText, denyText, rwText, roText,
      ));
      setSaveState("saved");
      window.setTimeout(() => setSaveState((p) => (p === "saved" ? "idle" : p)), 1500);
      setModal(null);
    } catch (err) {
      setModalError(err instanceof Error ? err.message : String(err));
    } finally {
      setModalBusy(false);
    }
  }

  return (
    <div id="sandbox-container">
      {modal && (
        <ToggleModal
          enabling={modal.enabling}
          hasPassword={hasDisablePassword}
          onConfirm={handleModalConfirm}
          onCancel={() => { setModal(null); setModalError(null); }}
          error={modalError}
          busy={modalBusy}
        />
      )}
      <div class="sandbox-header">
        <div>
          <h2>Sandbox</h2>
          <p>
            Default-deny outbound network policy plus explicit file
            bind-mounts for experiment runs and `the-lab-agent` launches.
            Built-in rules stay applied; user rules apply live to new sessions.
          </p>
        </div>
        <div class="sandbox-status-wrap">
          <label class="sandbox-toggle">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => {
                const target = e.target as HTMLInputElement;
                // Revert the browser's optimistic check — modal drives the change.
                target.checked = enabled;
                setModal({ enabling: !enabled });
                setModalError(null);
              }}
            />
            <span>{enabled ? "Experiments: On" : "Experiments: Off"}</span>
          </label>
          <div class="sandbox-status-note">
            {enabled
              ? "Sandbox on. A password is required to disable it."
              : "Sandbox off. A password will be set when enabling."}
          </div>
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

      <h3 class="sandbox-section-title">Network Access</h3>
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

      <h3 class="sandbox-section-title">File Access</h3>
      <p class="sandbox-section-note">
        Absolute paths, one per line. Anything not listed (and not in the
        built-in system set) is <em>invisible</em> to the sandboxed process.
        RW paths can be modified; RO paths are read-only. Note: writes
        happen as the sandbox's mapped sub-UID, so RW paths must allow
        writes from "others" (<code>chmod o+w</code>) or be owned by that UID.
      </p>
      <div class="sandbox-grid">
        <section class="sandbox-panel">
          <div class="sandbox-panel-title">Read-Write Paths</div>
          <div class="sandbox-panel-subtitle">Bind-mounted read-write.</div>
          <textarea
            value={rwText}
            onInput={(e) => setRwText((e.target as HTMLTextAreaElement).value)}
            placeholder={"/data/experiments\n/home/ubuntu/scratch"}
          />
        </section>

        <section class="sandbox-panel">
          <div class="sandbox-panel-title">Read-Only Paths</div>
          <div class="sandbox-panel-subtitle">Bind-mounted read-only.</div>
          <textarea
            value={roText}
            onInput={(e) => setRoText((e.target as HTMLTextAreaElement).value)}
            placeholder={"/opt/models\n/home/ubuntu/reference"}
          />
        </section>
      </div>

      <section class="sandbox-panel builtin-panel">
        <div class="sandbox-panel-title">Built-in File Binds</div>
        <div class="sandbox-panel-subtitle">
          Always applied: system libraries (read-only), repo &amp; agent credentials (read-write).
        </div>
        <div class="sandbox-chip-list">
          {builtinRw.map((path) => (
            <span class="sandbox-chip sandbox-chip-rw" key={`rw-${path}`} title="read-write">{path}</span>
          ))}
          {builtinRo.map((path) => (
            <span class="sandbox-chip sandbox-chip-ro" key={`ro-${path}`} title="read-only">{path}</span>
          ))}
          {builtinFileBinds.map((bind) => (
            <span
              class={`sandbox-chip sandbox-chip-${bind.mode}`}
              key={`sys-${bind.path}`}
              title={bind.mode === "ro" ? "system (read-only)" : "system (read-write)"}
            >
              {bind.path}
            </span>
          ))}
          {builtinRw.length === 0 && builtinRo.length === 0 && builtinFileBinds.length === 0 && (
            <span class="sandbox-empty">No built-in binds detected.</span>
          )}
        </div>
      </section>

      <section class="sandbox-panel observed-panel">
        <div class="sandbox-panel-title">Observed Network Accesses</div>
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
