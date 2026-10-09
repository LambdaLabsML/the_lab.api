/** The progress percentage to show for a running experiment: the run's
 *  wall-clock share (``run_pct``, added by the server from budget_remaining_s
 *  and MAX_WALL_SECONDS) when known, else what the script reports. The
 *  solver's ``pct_complete`` counts games picked up and reads 100 for the
 *  whole last stretch of a run. */
export function progressPct(p: Record<string, unknown> | null | undefined): number | undefined {
  if (!p) return undefined;
  for (const key of ["run_pct", "pct_complete", "pct"]) {
    const v = p[key];
    if (typeof v === "number" && Number.isFinite(v)) return v;
  }
  return undefined;
}
