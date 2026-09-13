# Local SQLite index

The Lab's SQLite index is a performance cache, never a database of record.
The authoritative state remains the JSON/files beneath
`<repo>/.the_lab/experiments/`: idea and experiment IDs, relationships,
statuses, timestamps, notes, metrics, scripts, and outputs all remain there.
Deleting every SQLite file therefore loses no Lab data.

## Location and safety

By default the index lives on the machine-local `/var/tmp` filesystem at
`/var/tmp/the-lab-<uid>/<sha256(canonical-repo-path + instance-id)>/index.sqlite3`.
Set `THE_LAB_INDEX_DIR` (or `the-lab index … --index-dir`) to choose another
*local* directory.  The directory is intentionally not in the repository:
many lab repositories live on NFS, where SQLite WAL locking is unsafe.  Linux
mountinfo is checked and an NFS/CIFS/SSHFS/9p location is refused with a
diagnostic rather than silently falling back to an unsafe DB.  Set
`THE_LAB_INDEX=off` to disable the optional cache.
The server checks canonical authoritative JSON paths periodically (default
60 seconds; `THE_LAB_INDEX_RECONCILE_SECONDS`, minimum 5) off the event loop.
Use `GET /api/v1/index/status` (or `the-lab index status`) for cache-only
diagnostics; index trouble never makes filesystem readiness fail by itself.

The identity metadata in the database includes the canonical repo path and
the Lab instance ID, so two repositories with the same instance ID cannot
share rows.  Schema versions are checked before reads.  SQLite lock waits are
bounded; a contention/corruption/schema error makes the cache unavailable,
not the filesystem store unavailable.

## Derived schema

`records` stores a JSON projection of authoritative `ideas` and `experiments`
plus a small normalized chart projection (identity, relation, status,
timestamps, descriptions, runtime, and tags).  Chart reads select those
columns rather than loading the opaque JSON payload, which may contain large
`meta` fields. `metric_values` holds numeric, finite metric leaves through the
legacy root→dict→dict dotted depth (for example `a.b.c`) for SQL metric filtering, chart projections, and
aggregations.  Booleans and non-finite values are omitted, matching the chart
data safety rules.  `metadata` contains schema, identity, and the monotonic
derived revision used to reject stale callback/reconciliation snapshots.
Notes, queues, messages, scripts, logs, and all other files are not made
authoritative by SQLite; they can continue to exist solely in their normal
filesystem formats.

## Rebuilding and recovery

Run either command:

```bash
the-lab index rebuild /path/to/repo
the-lab rebuild-index /path/to/repo
the-lab index status /path/to/repo
```

`rebuild` scans JSON directly and does **not** construct `Store`; consequently
it does not create `.the_lab`, an instance ID, or a git exclude entry.  It
writes only a temporary derived DB on the local cache volume, then atomically
publishes it.  Readers see either the prior complete cache or the new complete
cache, never a partially rebuilt one.

If an authoritative scan cannot enumerate a directory or parse a metadata
record, it fails rather than publishing a partial empty index. Server startup
also defers index replacement while Store reports source-load errors; requests
continue through the filesystem Store until a healthy reconciliation repairs
the derived cache.

On a read-only 442-idea / 1,349-experiment lab sample, an authoritative scan
took about 2.3 s and local rebuild about 0.43 s. A limited 250-row projected
history response was about 294 KB. The SQL query is cached by completed-data
revision; SQLite is intended to avoid repeated full filesystem/JSON work and
large history transfer, not to claim it beats an already-loaded in-memory
projection for every tiny warm query.

It is always safe to stop the server, delete the indicated `index.sqlite3`
(and `-wal`/`-shm` sidecars), and run rebuild.  Corrupt databases and schema
mismatches are quarantined by replacement on rebuild; source JSON is never
modified.  Back up the `.the_lab` files (on reliable storage), not SQLite.

After a successful filesystem commit, Store may best-effort upsert/delete its
matching derived row.  If that fails, the successful filesystem write remains
successful.  Startup and periodic full filesystem reconciliation repair a
crash between those steps as well as external file edits/deletions.  Requests
fall back to the ordinary in-memory/filesystem Store on any index failure and
surface the cache diagnostic separately from Lab readiness.
