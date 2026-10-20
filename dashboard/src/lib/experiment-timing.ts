/** Scheduler time is allocation time, not submission time or solver startup. */
export interface JobTiming {
  status: string; started_at?: string | null; actual_started_at?: string | null;
  attempt_started_at?: string | null; finished_at?: string | null;
  runtime_seconds?: number | null; attempt?: number; scheduler_state?: string | null;
  slurm_state?: string | null; meta?: { slurm_job_id?: string; slurm_state?: string };
}
export function jobWaiting(exp: JobTiming): boolean {
  return ["pending", "queued"].includes(exp.status) || ["PENDING", "PREEMPTED", "REQUEUED", "REQUEUE_HOLD"].includes(exp.scheduler_state || exp.slurm_state || exp.meta?.slurm_state || "");
}
export function jobStart(exp: JobTiming): string | null {
  if (jobWaiting(exp)) return null;
  return exp.attempt_started_at || exp.actual_started_at || (exp.meta?.slurm_job_id || exp.scheduler_state || exp.slurm_state ? null : exp.started_at || null);
}
export function jobRuntimeSeconds(exp: JobTiming, now = Date.now()): number | null {
  if (jobWaiting(exp)) return null;
  if (exp.status !== "running" && exp.runtime_seconds != null) return Math.max(0, exp.runtime_seconds);
  const start = jobStart(exp);
  const end = exp.finished_at ? Date.parse(exp.finished_at) : exp.status === "running" ? now : NaN;
  const seconds = start ? (end - Date.parse(start)) / 1000 : NaN;
  return Number.isFinite(seconds) ? Math.max(0, seconds) : null;
}
export function formatRunDuration(seconds: number): string {
  const s = Math.floor(seconds), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m ${String(s % 60).padStart(2, "0")}s` : m ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}
