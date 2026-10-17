/**
 * Writing needs a name: the composer asks for one before the first message
 * (one of the names already in use, or a new one) and then continues.
 * requireName(then) returns true when a name is set, else opens the dialog.
 */
import { useEffect, useState } from "preact/hooks";
import { signal } from "@preact/signals";
import { myName } from "../state/settings";
import { listHumans } from "../state/api";
import { humanDisplayName } from "../lib/identity";

const gate = signal<{ then?: () => void } | null>(null);

export function requireName(then?: () => void): boolean {
  if ((myName.value || "").trim()) return true;
  gate.value = { then };
  return false;
}

export function NameGate() {
  const open = gate.value;
  const [names, setNames] = useState<string[]>([]);
  const [choice, setChoice] = useState("");
  const [draft, setDraft] = useState("");
  useEffect(() => {
    if (!open) return;
    setChoice(""); setDraft("");
    listHumans()
      .then(h => setNames([...new Set(h.filter(x => x.name).map(x => humanDisplayName(x.id)))].sort()))
      .catch(() => setNames([]));
  }, [open]);
  if (!open) return null;
  const name = (choice === "__new" || !choice ? draft : choice).trim();
  const save = () => {
    if (!name) return;
    myName.value = name;
    const then = open.then;
    gate.value = null;
    if (then) window.setTimeout(then, 0);
  };
  return <>
    <button class="name-gate-scrim" aria-label="Cancel" tabIndex={-1} onClick={() => { gate.value = null; }} />
    <form class="name-gate" role="dialog" aria-modal="true" aria-label="Choose your name"
      onSubmit={e => { e.preventDefault(); save(); }}
      onKeyDown={e => { if (e.key === "Escape") gate.value = null; }}>
      <strong>Who's writing?</strong>
      <p>Messages are signed with your name (agents can address you as user:&lt;name&gt;). Pick yours once — it's remembered in this browser.</p>
      {names.length > 0 && (
        <label>Name in use
          <select value={choice} onChange={e => setChoice(e.currentTarget.value)} autoFocus>
            <option value="">— choose —</option>
            {names.map(n => <option key={n} value={n}>{n}</option>)}
            <option value="__new">a new name…</option>
          </select>
        </label>
      )}
      {(names.length === 0 || choice === "__new" || !choice) && (
        <label>{names.length ? "Or a new name" : "Your name"}
          <input value={draft} placeholder="e.g. David" maxLength={40} autoFocus={names.length === 0}
            onInput={e => { setDraft(e.currentTarget.value); if (choice && choice !== "__new") setChoice(""); }} />
        </label>
      )}
      <div class="name-gate-actions">
        <button type="button" class="ui-btn" onClick={() => { gate.value = null; }}>Cancel</button>
        <button class="ui-btn is-primary" disabled={!name}>Continue</button>
      </div>
    </form>
  </>;
}
