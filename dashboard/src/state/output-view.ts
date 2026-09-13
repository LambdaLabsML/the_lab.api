/** Pure output-view policy, shared by the detail UI and node tests. */
export function canShowExperimentOutput(status: string, hasOutput?: boolean): boolean {
  return Boolean(hasOutput) || ["starting", "running", "failed"].includes(status);
}

export function isOutputRefreshEvent(type: string): boolean {
  return [
    "experiment_log_updated", "experiment_output_updated",
    "experiment_progress_updated", "experiment_finished",
  ].includes(type);
}

export function outputUnavailableState(status: string, httpStatus?: number): "waiting" | "unavailable" | "error" {
  if (httpStatus === 404 && ["starting", "running", "pending", "queued"].includes(status)) return "waiting";
  if (httpStatus === 404 && ["failed", "completed", "cancelled"].includes(status)) return "unavailable";
  return "error";
}
