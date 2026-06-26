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
import { useDisclosure } from "../lib/hooks";
import { Badge, EmptyState, IconButton, Toggle, type BadgeTone } from "../components/ui";
import type {
  QueueExp,
  QueueSnapshot,
  ResourceState,
  ResourceUpsertBody,
} from "../lib/types";

// Recent-experiment status → design-language Badge tone.
const RECENT_STATUS_TONE: Record<string, BadgeTone> = {
  completed: "good",
  failed: "bad",
  cancelled: "neutral",
  running: "running",
};

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
  // Slurm-specific fields (assembled into executor_config on submit)
  slurm_ssh_host: string;
  slurm_partition: string;
  slurm_qos: string;
  slurm_account: string;
  slurm_ntasks: string;
  slurm_gpus: string;
  slurm_mem: string;
  slurm_time: string;
  slurm_git_repo_path: string;
  slurm_remote_base: string;
  slurm_slurm_conf: string;
  slurm_base_venv_path: string;
}

function resourceToFormState(r: ResourceState | null): ResourceFormState {
  const cfg: Record<string, unknown> = (r?.executor_config as Record<string, unknown>) ?? {};
  return {
    name: r?.name ?? "",
    kind: r?.kind ?? "local",
    unit_kind: r?.unit_kind ?? "gpu",
    capacity: String(r?.capacity ?? 1),
    jobs_per_unit: String(r?.jobs_per_unit ?? 1),
    tags: (r?.tags ?? []).join(","),
    slurm_ssh_host:       String(cfg.ssh_host       ?? ""),
    slurm_partition:      String(cfg.partition       ?? ""),
    slurm_qos:            String(cfg.qos             ?? ""),
    slurm_account:        String(cfg.account         ?? ""),
    slurm_ntasks:         cfg.ntasks   != null ? String(cfg.ntasks)   : "",
    slurm_gpus:           cfg.gpus     != null ? String(cfg.gpus)     : "",
    slurm_mem:            String(cfg.mem             ?? ""),
    slurm_time:           String(cfg.time            ?? ""),
    slurm_git_repo_path:  String(cfg.git_repo_path   ?? ""),
    slurm_remote_base:    String(cfg.remote_base     ?? ""),
    slurm_slurm_conf:     String(cfg.slurm_conf      ?? ""),
    slurm_base_venv_path: String(cfg.base_venv_path  ?? ""),
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
  const tags = form.tags.split(",").map((t) => t.trim()).filter(Boolean);

  let executor_config: Record<string, unknown> = {};
  if (form.kind === "slurm") {
    const s = form;
    if (s.slurm_ssh_host.trim())       executor_config.ssh_host        = s.slurm_ssh_host.trim();
    if (s.slurm_partition.trim())      executor_config.partition        = s.slurm_partition.trim();
    if (s.slurm_qos.trim())            executor_config.qos              = s.slurm_qos.trim();
    if (s.slurm_account.trim())        executor_config.account          = s.slurm_account.trim();
    if (s.slurm_ntasks.trim())         executor_config.ntasks           = parseInt(s.slurm_ntasks, 10);
    if (s.slurm_gpus.trim())           executor_config.gpus             = parseInt(s.slurm_gpus, 10);
    if (s.slurm_mem.trim())            executor_config.mem              = s.slurm_mem.trim();
    if (s.slurm_time.trim())           executor_config.time             = s.slurm_time.trim();
    if (s.slurm_git_repo_path.trim())  executor_config.git_repo_path    = s.slurm_git_repo_path.trim();
    if (s.slurm_remote_base.trim())    executor_config.remote_base      = s.slurm_remote_base.trim();
    if (s.slurm_slurm_conf.trim())     executor_config.slurm_conf       = s.slurm_slurm_conf.trim();
    if (s.slurm_base_venv_path.trim()) executor_config.base_venv_path   = s.slurm_base_venv_path.trim();
  }

  return {
    ok: true,
    body: {
      name,
      kind: form.kind || "local",
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

  const f = (key: keyof ResourceFormState) => (e: Event) =>
    setForm({ ...form, [key]: (e.target as HTMLInputElement).value });

  return (
    <form class="queue-resource-form" onSubmit={handleSubmit}>
      {/* ── Row 1: name / kind / unit_kind ── */}
      <div class="queue-form-row">
        <label>
          <span>Name</span>
          <input type="text" value={form.name} disabled={!isNew}
            onInput={f("name")} placeholder="slurm-h100" />
        </label>
        <label>
          <span>Kind</span>
          <select value={form.kind}
            onChange={(e) => setForm({ ...form, kind: (e.target as HTMLSelectElement).value })}>
            <option value="local">local</option>
            <option value="slurm">slurm</option>
          </select>
        </label>
        <label>
          <span>Unit kind</span>
          <select value={form.unit_kind}
            onChange={(e) => setForm({ ...form, unit_kind: (e.target as HTMLSelectElement).value })}>
            <option value="gpu">gpu</option>
            <option value="cpu">cpu</option>
            <option value="none">none</option>
          </select>
        </label>
      </div>

      {/* ── Row 2: capacity / jobs_per_unit / tags ── */}
      <div class="queue-form-row">
        <label>
          <span>Capacity</span>
          <input type="number" min={1} step={1} value={form.capacity} onInput={f("capacity")} />
        </label>
        <label>
          <span>jobs_per_unit</span>
          <input type="number" min={0.05} max={1} step={0.05} value={form.jobs_per_unit}
            onInput={f("jobs_per_unit")} />
        </label>
        <label class="queue-form-tags">
          <span>Tags (comma-separated)</span>
          <input type="text" value={form.tags} onInput={f("tags")} placeholder="h100,80gb" />
        </label>
      </div>

      {/* ── Slurm executor config ── */}
      {form.kind === "slurm" && (
        <div class="queue-form-slurm">
          <div class="queue-form-slurm-label">Slurm executor</div>
          <div class="queue-form-row">
            <label>
              <span>SSH host</span>
              <input type="text" value={form.slurm_ssh_host} onInput={f("slurm_ssh_host")}
                placeholder="slurm (default)" />
            </label>
            <label>
              <span>Partition</span>
              <input type="text" value={form.slurm_partition} onInput={f("slurm_partition")}
                placeholder="lowprio (default)" />
            </label>
            <label>
              <span>QOS</span>
              <input type="text" value={form.slurm_qos} onInput={f("slurm_qos")}
                placeholder="(defaults to partition)" />
            </label>
            <label>
              <span>Account</span>
              <input type="text" value={form.slurm_account} onInput={f("slurm_account")}
                placeholder="optional" />
            </label>
          </div>
          <div class="queue-form-row">
            <label>
              <span>ntasks</span>
              <input type="number" min={1} step={1} value={form.slurm_ntasks}
                onInput={f("slurm_ntasks")} placeholder="1 (default)" />
            </label>
            <label>
              <span>GPUs per job</span>
              <input type="number" min={0} step={1} value={form.slurm_gpus}
                onInput={f("slurm_gpus")} placeholder="1 (default)" />
            </label>
            <label>
              <span>Memory (--mem)</span>
              <input type="text" value={form.slurm_mem} onInput={f("slurm_mem")}
                placeholder="e.g. 80G" />
            </label>
            <label>
              <span>Time limit (--time)</span>
              <input type="text" value={form.slurm_time} onInput={f("slurm_time")}
                placeholder="e.g. 12:00:00" />
            </label>
          </div>
          <div class="queue-form-row">
            <label class="queue-form-tags">
              <span>Remote git repo path</span>
              <input type="text" value={form.slurm_git_repo_path}
                onInput={f("slurm_git_repo_path")} placeholder="~/.thelab/repo.git (default)" />
            </label>
            <label class="queue-form-tags">
              <span>Remote job base dir</span>
              <input type="text" value={form.slurm_remote_base}
                onInput={f("slurm_remote_base")} placeholder="~/.thelab/jobs (default)" />
            </label>
          </div>
          <div class="queue-form-row">
            <label class="queue-form-tags">
              <span>slurm.conf path on remote</span>
              <input type="text" value={form.slurm_slurm_conf}
                onInput={f("slurm_slurm_conf")} placeholder="/data/slurm/etc/slurm.conf (default)" />
            </label>
            <label class="queue-form-tags">
              <span>Shared venv path (optional)</span>
              <input type="text" value={form.slurm_base_venv_path}
                onInput={f("slurm_base_venv_path")} placeholder="e.g. /shared/.venv" />
            </label>
          </div>
        </div>
      )}

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
  const { open: showAddResource, onOpen: openAddResource, onClose: closeAddResource } = useDisclosure(false);
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
      if (isNew) closeAddResource();
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
      <div class="pane-bar">
        <span class="ui-eyebrow pane-bar-title">Queue</span>
        <span class="pane-bar-count">{loaded ? summary : "…"}</span>
        <div class="pane-bar-actions">
          <Toggle
            active={!!snapshot?.config.paused}
            onClick={() => { if (snapshot && !busy) handleTogglePause(); }}
            title={snapshot?.config.paused ? "Queue is paused — click to resume" : "Pause the queue"}
          >
            {snapshot?.config.paused ? "paused" : "running"}
          </Toggle>
          <IconButton onClick={() => refresh()} disabled={busy} title="Refresh">↺</IconButton>
        </div>
      </div>

      {error && <div class="queue-error">{error}</div>}

      {/* Dispatch interval — tucked in a collapsed Advanced section */}
      <details class="queue-advanced">
        <summary>Advanced</summary>
        <label class="queue-interval" style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: "var(--text-xs)", color: "var(--text-muted)", padding: "4px 0" }}>
          <span>dispatch_interval_s</span>
          <input
            type="number" min={0.1} step={0.1} value={intervalDraft}
            disabled={!snapshot || busy}
            onInput={(e) => setIntervalDraft((e.target as HTMLInputElement).value)}
            onBlur={commitInterval}
            onKeyDown={(e) => { if ((e as KeyboardEvent).key === "Enter") (e.target as HTMLInputElement).blur(); }}
          />
        </label>
      </details>

      <section class="queue-section">
        <div class="queue-section-header">
          <span class="ui-eyebrow">Resources</span>
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
            <EmptyState title="No resources" body="No resources configured." />
          )}
        </div>
        {showAddResource ? (
          <div class="queue-add-form-wrap">
            <ResourceForm
              initial={null}
              isNew={true}
              onCancel={closeAddResource}
              onSubmit={(body) => handleSubmitResource(body, true)}
            />
          </div>
        ) : (
          <button
            class="queue-add-btn"
            onClick={openAddResource}
            disabled={busy}
          >
            + Add resource
          </button>
        )}
      </section>

      <section class="queue-section queue-lists">
        <div class="queue-list-col">
          <div class="queue-section-header">
            <span class="ui-eyebrow">Queued</span>
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
              <EmptyState title="Queue empty" body="No experiments queued." />
            )}
          </div>
        </div>

        <div class="queue-list-col">
          <div class="queue-section-header">
            <span class="ui-eyebrow">Running</span>
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
              <EmptyState title="Idle" body="Nothing running." />
            )}
          </div>
        </div>
      </section>

      <section class="queue-section">
        <div class="queue-section-header">
          <span class="ui-eyebrow">Recent</span>
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
                    <Badge tone={RECENT_STATUS_TONE[status] ?? "neutral"}>{status}</Badge>
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
            <EmptyState title="Nothing finished" body="No finished experiments yet." />
          )}
        </div>
      </section>
    </div>
  );
}
