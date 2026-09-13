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
