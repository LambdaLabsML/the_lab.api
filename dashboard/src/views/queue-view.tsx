import { useEffect, useRef, useState } from "preact/hooks";
import {
  cancelExperiment,
  deleteResource,
  getQueue,
  pauseQueue,
  resumeQueue,
  setExperimentPriority,
  setQueueConfig,
  upsertResource,
} from "../state/api";
import { selectedIdea } from "../state/settings";
import type {
  QueueExp,
  QueueSnapshot,
  ResourceState,
  ResourceUpsertBody,
} from "../lib/types";

// dynamic — driven by holder index; hex required for template-string style injection
const HOLDER_PALETTE = [
  "#3fb950", // green
  "#79c0ff", // blue
  "#d29922", // amber
  "#bc8cff", // violet
  "#f78166", // coral
  "#56d4dd", // teal
  "#f0883e", // orange
  "#a5d6ff", // sky
];

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "--";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 48) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

function truncate(s: string, max: number): string {
  if (!s) return "";
  return s.length > max ? s.substring(0, max - 1) + "…" : s;
}

function summariseRequirements(req: QueueExp["requirements"]): string {
  if (!req) return "(default)";
  const parts: string[] = [];
  if (typeof req.units === "number") parts.push(`${req.units} units`);
  if (req.kind && req.kind !== "any") parts.push(req.kind);
  if (req.tags && req.tags.length > 0) parts.push(req.tags.join(","));
  if (parts.length === 0) return "(default)";
  return parts.join(" · ");
}

interface ResourceFormState {
  name: string;
  kind: string;
  unit_kind: string;
  capacity: string;
  jobs_per_unit: string;
  tags: string;
  executor_config: string;
}

function resourceToFormState(r: ResourceState | null): ResourceFormState {
  return {
    name: r?.name ?? "",
    kind: r?.kind ?? "local",
    unit_kind: r?.unit_kind ?? "gpu",
    capacity: String(r?.capacity ?? 1),
    jobs_per_unit: String(r?.jobs_per_unit ?? 1),
    tags: (r?.tags ?? []).join(","),
    executor_config: r ? JSON.stringify(r.executor_config ?? {}) : "{}",
  };
}

function validateForm(
  form: ResourceFormState,
  isNew: boolean,
): { ok: true; body: ResourceUpsertBody } | { ok: false; error: string } {
  const name = form.name.trim();
  if (!name) return { ok: false, error: "Name is required." };
  const capacity = Number.parseInt(form.capacity, 10);
  if (!Number.isFinite(capacity) || capacity < 1) {
    return { ok: false, error: "Capacity must be ≥ 1." };
  }
  const jpu = Number.parseFloat(form.jobs_per_unit);
  if (!Number.isFinite(jpu) || jpu <= 0 || jpu > 1.0) {
    return { ok: false, error: "jobs_per_unit must be > 0 and ≤ 1.0." };
  }
  let executor_config: Record<string, unknown> = {};
  if (form.executor_config.trim()) {
    try {
      const parsed = JSON.parse(form.executor_config);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        executor_config = parsed as Record<string, unknown>;
      } else {
        return { ok: false, error: "executor_config must be a JSON object." };
      }
    } catch {
      return { ok: false, error: "executor_config is not valid JSON." };
    }
  }
  const tags = form.tags
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  return {
    ok: true,
    body: {
      name,
      kind: form.kind.trim() || "local",
      unit_kind: form.unit_kind.trim() || "none",
      capacity,
      jobs_per_unit: jpu,
      tags,
      executor_config,
    },
  };
}

function unitsLabel(units: number[] | null | undefined): string {
  if (!units || units.length === 0) return "[]";
  return `[${units.join(",")}]`;
}

function CapacityBar({ resource }: { resource: ResourceState }) {
  const cap = Math.max(1, resource.utilization.capacity);
  const segments: { label: string; pct: number; color: string }[] = [];
  let used = 0;
  resource.utilization.holders.forEach((h, i) => {
    const n = h.units.length;
    if (n <= 0) return;
    used += n;
    segments.push({
      label: `${h.experiment_label}: ${unitsLabel(h.units)}`,
      pct: (n / cap) * 100,
      color: HOLDER_PALETTE[i % HOLDER_PALETTE.length],
    });
  });
  const freePct = Math.max(0, ((cap - used) / cap) * 100);
  return (
    <div class="queue-capbar">
      <div class="queue-capbar-track">
        {segments.map((s, i) => (
          <div
            key={i}
            class="queue-capbar-seg"
            style={`width:${s.pct}%; background:${s.color};`}
            title={s.label}
          />
        ))}
        {freePct > 0 && (
          <div
            class="queue-capbar-seg queue-capbar-free"
            style={`width:${freePct}%`}
            title={`free: ${resource.utilization.free_units} units`}
          />
        )}
      </div>
      <div class="queue-capbar-numbers">
        <span class="queue-capbar-frac">
          {resource.utilization.in_use_units} / {resource.utilization.capacity}
        </span>
        <span class="queue-capbar-meta">
          {resource.utilization.running_jobs} / {resource.utilization.max_parallel_jobs} jobs ·
          default {resource.utilization.default_units_per_job} units/job ·
          jobs_per_unit={resource.jobs_per_unit}
        </span>
      </div>
    </div>
  );
}

function ResourceForm({
  initial,
  isNew,
  onCancel,
  onSubmit,
}: {
  initial: ResourceState | null;
  isNew: boolean;
  onCancel: () => void;
  onSubmit: (body: ResourceUpsertBody) => void | Promise<void>;
}) {
  const [form, setForm] = useState<ResourceFormState>(resourceToFormState(initial));
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function handleSubmit(e: Event) {
    e.preventDefault();
    const v = validateForm(form, isNew);
    if (!v.ok) {
      setErr(v.error);
      return;
    }
    setErr(null);
    setBusy(true);
    Promise.resolve(onSubmit(v.body)).finally(() => setBusy(false));
  }

  return (
    <form class="queue-resource-form" onSubmit={handleSubmit}>
      <div class="queue-form-row">
        <label>
          <span>Name</span>
          <input
            type="text"
            value={form.name}
            disabled={!isNew}
            onInput={(e) => setForm({ ...form, name: (e.target as HTMLInputElement).value })}
            placeholder="local-h100"
          />
        </label>
        <label>
          <span>Kind</span>
          <input
            type="text"
            value={form.kind}
            onInput={(e) => setForm({ ...form, kind: (e.target as HTMLInputElement).value })}
            placeholder="local"
          />
        </label>
        <label>
          <span>Unit kind</span>
          <input
            type="text"
            value={form.unit_kind}
            onInput={(e) =>
              setForm({ ...form, unit_kind: (e.target as HTMLInputElement).value })
            }
            placeholder="gpu | cpu | none"
          />
        </label>
      </div>
      <div class="queue-form-row">
        <label>
          <span>Capacity</span>
          <input
            type="number"
            min={1}
            step={1}
            value={form.capacity}
            onInput={(e) =>
              setForm({ ...form, capacity: (e.target as HTMLInputElement).value })
            }
          />
        </label>
        <label>
          <span>jobs_per_unit</span>
          <input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={form.jobs_per_unit}
            onInput={(e) =>
              setForm({ ...form, jobs_per_unit: (e.target as HTMLInputElement).value })
            }
          />
        </label>
        <label class="queue-form-tags">
          <span>Tags (comma-separated)</span>
          <input
            type="text"
            value={form.tags}
            onInput={(e) => setForm({ ...form, tags: (e.target as HTMLInputElement).value })}
            placeholder="h100,80gb"
          />
        </label>
      </div>
      <label class="queue-form-cfg">
        <span>executor_config (JSON)</span>
        <input
          type="text"
          value={form.executor_config}
          onInput={(e) =>
            setForm({ ...form, executor_config: (e.target as HTMLInputElement).value })
          }
          placeholder='{}'
        />
      </label>
      {err && <div class="queue-form-error">{err}</div>}
      <div class="queue-form-actions">
        <button type="submit" class="queue-btn primary" disabled={busy}>
          {busy ? "Saving…" : isNew ? "Add resource" : "Save"}
        </button>
        <button type="button" class="queue-btn" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function QueueView() {
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editingResource, setEditingResource] = useState<string | null>(null);
  const [showAddResource, setShowAddResource] = useState(false);
  const [intervalDraft, setIntervalDraft] = useState<string>("");
  const [, setTick] = useState(0);

  const cancelledRef = useRef(false);

  async function refresh(): Promise<QueueSnapshot | null> {
    try {
      const snap = await getQueue();
      if (cancelledRef.current) return null;
      setSnapshot(snap);
      setLoaded(true);
      setError(null);
      // Sync interval draft with server unless user is editing
      setIntervalDraft((prev) => {
        if (prev === "") return String(snap.config.dispatch_interval_s);
        return prev;
      });
      return snap;
    } catch (err) {
      if (cancelledRef.current) return null;
      setError(err instanceof Error ? err.message : String(err));
      setLoaded(true);
      return null;
    }
  }

  useEffect(() => {
    cancelledRef.current = false;
    refresh();
    const poll = window.setInterval(refresh, 3000);
    const tick = window.setInterval(() => setTick((n) => n + 1), 30000);
    return () => {
      cancelledRef.current = true;
      window.clearInterval(poll);
      window.clearInterval(tick);
    };
  }, []);

  async function withBusy<T>(fn: () => Promise<T>): Promise<T | null> {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return null;
    } finally {
      if (!cancelledRef.current) setBusy(false);
    }
  }

  async function handleTogglePause() {
    if (!snapshot) return;
    await withBusy(async () => {
      if (snapshot.config.paused) {
        await resumeQueue();
      } else {
        await pauseQueue();
      }
      await refresh();
    });
  }

  async function commitInterval() {
    if (!snapshot) return;
    const v = Number.parseFloat(intervalDraft);
    if (!Number.isFinite(v) || v <= 0) {
      setIntervalDraft(String(snapshot.config.dispatch_interval_s));
      return;
    }
    if (v === snapshot.config.dispatch_interval_s) return;
    await withBusy(async () => {
      await setQueueConfig({ dispatch_interval_s: v });
      await refresh();
    });
  }

  async function handleBumpPriority(exp: QueueExp, delta: number) {
    const next = (exp.priority ?? 0) + delta;
    await withBusy(async () => {
      await setExperimentPriority(exp.label, next);
      await refresh();
    });
  }

  async function handleSetPriority(exp: QueueExp) {
    const cur = exp.priority ?? 0;
    const raw = window.prompt(`Set priority for exp/${exp.label}`, String(cur));
    if (raw === null) return;
    const next = Number.parseInt(raw, 10);
    if (!Number.isFinite(next)) return;
    await withBusy(async () => {
      await setExperimentPriority(exp.label, next);
      await refresh();
    });
  }

  async function handleCancel(exp: QueueExp) {
    if (!window.confirm(`Cancel exp/${exp.label}?`)) return;
    await withBusy(async () => {
      await cancelExperiment(exp.label);
      await refresh();
    });
  }

  async function handleDeleteResource(r: ResourceState) {
    if (!window.confirm(`Delete resource "${r.name}"?\n\nThis cannot be undone.`)) return;
    await withBusy(async () => {
      await deleteResource(r.name);
      await refresh();
    });
  }

  async function handleSubmitResource(body: ResourceUpsertBody, isNew: boolean) {
    await withBusy(async () => {
      await upsertResource(body.name, body);
      if (isNew) setShowAddResource(false);
      else setEditingResource(null);
      await refresh();
    });
  }

  function handleOpenIdea(ideaId: number) {
    selectedIdea.value = ideaId;
  }

  const queued = snapshot?.queued ?? [];
  const running = snapshot?.running ?? [];
  const recent = snapshot?.recent ?? [];
  const resources = snapshot?.resources ?? [];

  const summary = snapshot
    ? `${queued.length} queued · ${running.length} running · ${resources.length} resource${
        resources.length === 1 ? "" : "s"
      }`
    : "Loading…";

  return (
    <div id="queue-container">
      <div class="queue-header">
        <div class="queue-header-left">
          <h2>Queue</h2>
          <p>
            Experiment dispatch queue. Adjust priorities, pause the dispatcher,
            and manage resource pools (each pool is a set of unit slots like GPU
            indices).
          </p>
        </div>
        <div class="queue-header-right">
          <div class="queue-summary">{loaded ? summary : "Loading…"}</div>
          <div class="queue-controls">
            <label class="queue-toggle">
              <input
                type="checkbox"
                checked={!!snapshot?.config.paused}
                disabled={!snapshot || busy}
                onChange={handleTogglePause}
              />
              <span>Paused</span>
            </label>
            <label class="queue-interval">
              <span>dispatch_interval_s</span>
              <input
                type="number"
                min={0.1}
                step={0.1}
                value={intervalDraft}
                disabled={!snapshot || busy}
                onInput={(e) => setIntervalDraft((e.target as HTMLInputElement).value)}
                onBlur={commitInterval}
                onKeyDown={(e) => {
                  if ((e as KeyboardEvent).key === "Enter") {
                    (e.target as HTMLInputElement).blur();
                  }
                }}
              />
            </label>
            <button
              class="queue-btn"
              onClick={() => refresh()}
              disabled={busy}
              title="Reload queue"
            >
              ↻ Refresh
            </button>
          </div>
        </div>
      </div>

      {error && <div class="queue-error">{error}</div>}

      <section class="queue-section">
        <div class="queue-section-header">
          <h3>Resources</h3>
          <span class="queue-section-meta">{resources.length} pool{resources.length === 1 ? "" : "s"}</span>
        </div>
        <div class="queue-resources-grid">
          {resources.map((r) => {
            const isEditing = editingResource === r.name;
            return (
              <section class="queue-resource-card" key={r.name}>
                <div class="queue-resource-top">
                  <div class="queue-resource-id">
                    <code class="queue-resource-name">{r.name}</code>
                    <span class="queue-chip">{r.kind}</span>
                    <span class="queue-chip queue-chip-unit">{r.unit_kind}</span>
                  </div>
                  <div class="queue-resource-tags">
                    {r.tags.map((t) => (
                      <span class="queue-tag" key={t}>
                        {t}
                      </span>
                    ))}
                  </div>
                </div>

                <CapacityBar resource={r} />

                {r.utilization.holders.length > 0 && (
                  <div class="queue-holder-list">
                    {r.utilization.holders.map((h, i) => (
                      <div class="queue-holder-row" key={`${h.experiment_label}-${i}`}>
                        <span
                          class="queue-holder-swatch"
                          style={`background:${HOLDER_PALETTE[i % HOLDER_PALETTE.length]}`}
                        />
                        <code class="queue-holder-label">exp/{h.experiment_label}</code>
                        <span class="queue-holder-units">{unitsLabel(h.units)}</span>
                      </div>
                    ))}
                  </div>
                )}

                {isEditing ? (
                  <ResourceForm
                    initial={r}
                    isNew={false}
                    onCancel={() => setEditingResource(null)}
                    onSubmit={(body) => handleSubmitResource(body, false)}
                  />
                ) : (
                  <div class="queue-resource-actions">
                    <button
                      class="queue-btn"
                      disabled={busy}
                      onClick={() => setEditingResource(r.name)}
                    >
                      Edit
                    </button>
                    <button
                      class="queue-btn danger"
                      disabled={busy}
                      onClick={() => handleDeleteResource(r)}
                    >
                      Delete
                    </button>
                  </div>
                )}
              </section>
            );
          })}
          {resources.length === 0 && loaded && (
            <div class="queue-empty">No resources configured.</div>
          )}
        </div>
        {showAddResource ? (
          <div class="queue-add-form-wrap">
            <ResourceForm
              initial={null}
              isNew={true}
              onCancel={() => setShowAddResource(false)}
              onSubmit={(body) => handleSubmitResource(body, true)}
            />
          </div>
        ) : (
          <button
            class="queue-add-btn"
            onClick={() => setShowAddResource(true)}
            disabled={busy}
          >
            + Add resource
          </button>
        )}
      </section>

      <section class="queue-section queue-lists">
        <div class="queue-list-col">
          <div class="queue-section-header">
            <h3>Queued</h3>
            <span class="queue-section-meta">{queued.length}</span>
          </div>
          <div class="queue-list">
            {queued.map((exp) => (
              <div class="queue-row queued" key={exp.id}>
                <div class="queue-row-main">
                  <div class="queue-row-label-line">
                    <code class="queue-row-label">exp/{exp.label}</code>
                    <button
                      class="queue-link"
                      onClick={() => handleOpenIdea(exp.idea_id)}
                      title={`Open idea ${exp.idea_id}`}
                    >
                      idea {exp.idea_id}
                    </button>
                  </div>
                  <div class="queue-row-desc" title={exp.description}>
                    {truncate(exp.description, 140)}
                  </div>
                  <div class="queue-row-meta">
                    <span class="queue-meta-item">
                      req: {summariseRequirements(exp.requirements)}
                    </span>
                    {exp.depends_on.length > 0 && (
                      <span class="queue-meta-item">
                        deps:{" "}
                        {exp.depends_on.map((d) => (
                          <span class="queue-dep-chip" key={d}>
                            {d}
                          </span>
                        ))}
                      </span>
                    )}
                    <span class="queue-meta-item" title={exp.created_at}>
                      created {relativeTime(exp.created_at)}
                    </span>
                  </div>
                </div>
                <div class="queue-row-side">
                  <div class="queue-priority">
                    <button
                      class="queue-btn icon"
                      title="Increase priority"
                      disabled={busy}
                      onClick={() => handleBumpPriority(exp, 1)}
                    >
                      ↑
                    </button>
                    <button
                      class="queue-priority-value"
                      title="Click to set priority"
                      disabled={busy}
                      onClick={() => handleSetPriority(exp)}
                    >
                      {exp.priority}
                    </button>
                    <button
                      class="queue-btn icon"
                      title="Decrease priority"
                      disabled={busy}
                      onClick={() => handleBumpPriority(exp, -1)}
                    >
                      ↓
                    </button>
                  </div>
                  <button
                    class="queue-btn danger small"
                    disabled={busy}
                    onClick={() => handleCancel(exp)}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ))}
            {queued.length === 0 && loaded && (
              <div class="queue-empty">No experiments queued.</div>
            )}
          </div>
        </div>

        <div class="queue-list-col">
          <div class="queue-section-header">
            <h3>Running</h3>
            <span class="queue-section-meta">{running.length}</span>
          </div>
          <div class="queue-list">
            {running.map((exp) => (
              <div class="queue-row running" key={exp.id}>
                <div class="queue-row-main">
                  <div class="queue-row-label-line">
                    <code class="queue-row-label">exp/{exp.label}</code>
                    <button
                      class="queue-link"
                      onClick={() => handleOpenIdea(exp.idea_id)}
                      title={`Open idea ${exp.idea_id}`}
                    >
                      idea {exp.idea_id}
                    </button>
                  </div>
                  <div class="queue-row-desc" title={exp.description}>
                    {truncate(exp.description, 140)}
                  </div>
                  <div class="queue-row-meta">
                    {exp.assigned_resource && (
                      <span class="queue-meta-item">
                        on{" "}
                        <code class="queue-row-resource">{exp.assigned_resource}</code>:{" "}
                        <code class="queue-row-units">{unitsLabel(exp.assigned_units)}</code>
                      </span>
                    )}
                    <span class="queue-meta-item" title={exp.started_at ?? ""}>
                      started {relativeTime(exp.started_at)}
                    </span>
                  </div>
                </div>
                <div class="queue-row-side">
                  <button
                    class="queue-btn danger small"
                    disabled={busy}
                    onClick={() => handleCancel(exp)}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ))}
            {running.length === 0 && loaded && (
              <div class="queue-empty">Nothing running.</div>
            )}
          </div>
        </div>
      </section>

      <section class="queue-section">
        <div class="queue-section-header">
          <h3>Recent</h3>
          <span class="queue-section-meta">{recent.length}</span>
        </div>
        <div class="queue-list">
          {recent.map((exp) => {
            const status = (exp.status || "").toLowerCase();
            const score = exp.metrics && typeof (exp.metrics as Record<string, unknown>).score === "number"
              ? ((exp.metrics as Record<string, number>).score)
              : null;
            return (
              <div class={`queue-row recent ${status}`} key={String(exp.id)}>
                <div class="queue-row-main">
                  <div class="queue-row-label-line">
                    <code class="queue-row-label">exp/{exp.label}</code>
                    <span class="agents-chip" style={{ textTransform: "uppercase" }}>{status}</span>
                    <button
                      class="queue-link"
                      onClick={() => handleOpenIdea(exp.idea_id)}
                      title={`Open idea ${exp.idea_id}`}
                    >
                      idea {exp.idea_id}
                    </button>
                  </div>
                  <div class="queue-row-desc" title={exp.description}>
                    {truncate(exp.description, 140)}
                  </div>
                  <div class="queue-row-meta">
                    {score != null && (
                      <span class="queue-meta-item">
                        score: <code>{score.toFixed(4)}</code>
                      </span>
                    )}
                    {exp.error && (
                      <span class="queue-meta-item" style={{ color: "var(--red)" }} title={exp.error}>
                        error: {truncate(exp.error, 80)}
                      </span>
                    )}
                    {exp.assigned_resource && (
                      <span class="queue-meta-item">
                        ran on <code>{exp.assigned_resource}</code>
                      </span>
                    )}
                    {exp.finished_at && (
                      <span class="queue-meta-item" title={exp.finished_at}>
                        finished {relativeTime(exp.finished_at)}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
          {recent.length === 0 && loaded && (
            <div class="queue-empty">No finished experiments yet.</div>
          )}
        </div>
      </section>
    </div>
  );
}
