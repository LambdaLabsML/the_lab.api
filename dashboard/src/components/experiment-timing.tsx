import { signal } from "@preact/signals";
import { useEffect } from "preact/hooks";
import { jobWaiting, jobStart, jobRuntimeSeconds, formatRunDuration, type JobTiming } from "../lib/experiment-timing";

// A single clock for all visible running rows, released when none need it.
const now = signal(Date.now());
let watchers = 0, clock: ReturnType<typeof setInterval> | undefined;
function useJobClock(active: boolean) {
  useEffect(() => {
    if (!active) return;
    if (watchers++ === 0) { now.value = Date.now(); clock = setInterval(() => { now.value = Date.now(); }, 1000); }
    return () => { if (--watchers === 0) { clearInterval(clock); clock = undefined; } };
  }, [active]);
  return active ? now.value : Date.now();
}
export function ExperimentStart({ exp }: { exp: JobTiming }) {
  const start = jobStart(exp);
  return start ? <time dateTime={start} title={`Job start${(exp.attempt || 1) > 1 ? ` · attempt ${exp.attempt}` : ""}: ${new Date(start).toLocaleString()}`}>
    {new Date(start).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" })}
  </time> : <span class="muted">{jobWaiting(exp) ? "Waiting" : "—"}</span>;
}
export function ExperimentRuntime({ exp }: { exp: JobTiming }) {
  const tick = useJobClock(exp.status === "running" && !jobWaiting(exp) && !!jobStart(exp));
  const seconds = jobRuntimeSeconds(exp, tick);
  const description = jobStart(exp)
    ? `Runtime of ${exp.attempt && exp.attempt > 1 ? `attempt ${exp.attempt}` : "this job"}; excludes scheduler queue waiting`
    : "Legacy runtime measured from dispatch; actual job start is unavailable";
  return <span class="experiment-runtime" title={description}>
    {seconds == null ? "—" : formatRunDuration(seconds)}
  </span>;
}
