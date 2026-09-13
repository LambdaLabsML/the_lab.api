# Performance and reliability work

Filesystem JSON records, scripts, logs, and artifacts remain authoritative.
The local SQLite index planned in step 5 must be disposable and rebuildable.

## 1. Keep blocking work off the event loop

Filesystem mutations and reads in async request handlers, runner/scheduler
operations, notification delivery, and performance logging use a bounded
worker pool. `THE_LAB_IO_WORKERS` controls concurrency (default 8).
Writes retain the store's locking/order guarantees and are awaited before
acknowledgement. Scheduler wakeups are handed to the owning loop safely.
Chat uses the asynchronous client and closes it when streaming finishes.

`GET /api/v1/health` includes event-loop lag p95/max over the last 300 samples
(one per second). For request timing, start with `--perf /local/path/api.csv`;
choose local storage so diagnostics do not contend with the lab's NFS mount.
The CSV measures middleware response preparation, not browser rendering or
complete network transfer. Correlate it with browser timings and loop lag.

Validation: `python -m unittest discover -s tests -v` includes a real ASGI
request test that injects slow experiment writes while probing health, a
write/reload check, bounded worker concurrency, and error propagation.

## 2. Reduce dashboard refresh work

Completed chart rows have a separate revision/cache; updates to running jobs
do not rebuild completed history. `/chart-data` accepts `limit`, `metrics`, and
`compact`; omitted options preserve full history. Metric discovery always
includes the full completed history. The UI initially requests 250 completed
runs and the metrics needed by its open charts/table, plus running progress.
The top bar identifies the partial window and offers older/all history. Chart
improvement comparisons apply to the loaded window; load all for global history.
Table columns still default to all available top-level metrics; hiding columns
reduces that view's requested metric set. Running progress is included in the
chart response, eliminating a per-running-experiment request fanout.

Dashboard requests bypass notification injection/buffering. Agent requests
retain notifications. Validation covers metric projection, history limits,
live progress freshness, completed-cache invalidation, and the dashboard bypass.
The production dashboard build passes. Type checking has 42 existing baseline
errors; this step adds none (compared by file and diagnostic, excluding shifted
line numbers).

## 3. Bound dashboard requests and recover visibly

Ordinary API requests have a 15-second deadline covering both headers and body.
Transient GET failures retry at most twice with bounded backoff; authentication
and JSON parse failures do not retry. Mutations are attempted exactly once.
A mutation timeout explicitly warns that its outcome is unknown: refresh state
before deciding whether to repeat it. Intentional streaming requests retain
their streaming lifecycle rather than an ordinary JSON-request deadline.

Recurring requests coalesce while in flight, release their guard on failure,
and resume on later ticks. Component-owned requests can be cancelled on cleanup.
Existing data remains visible after a failed refresh; the top bar shows failed
endpoints, the last successful API update, and a manual retry action.
Run the bundled TypeScript behavior tests with `cd dashboard && npm test`.
