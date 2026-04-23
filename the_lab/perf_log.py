"""Optional per-request performance log (CSV).

Enabled by setting the ``THE_LAB_PERF_LOG`` environment variable to a file
path. When set, every API call the middleware sees writes one row with
timing, source, sizes, and status. No aggregation — the raw CSV is meant
for offline analysis (pandas, spreadsheet, etc.).
"""
from __future__ import annotations

import csv
import datetime as _dt
import os
import threading
from pathlib import Path

_COLUMNS = [
    "timestamp",
    "method",
    "path",
    "normalized_path",
    "status",
    "duration_ms",
    "source",
    "query",
    "body_bytes",
    "response_bytes",
    "client_ip",
]

_lock = threading.Lock()
_writer: csv.DictWriter | None = None
_fh = None
_path: Path | None = None


def _ensure_open(path: Path) -> csv.DictWriter:
    global _writer, _fh, _path
    if _writer is not None and _path == path:
        return _writer
    # Rotate if path changed (different repo / re-init mid-process)
    if _fh is not None:
        try:
            _fh.close()
        except Exception:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    _fh = open(path, "a", newline="", buffering=1)  # line-buffered
    _writer = csv.DictWriter(_fh, fieldnames=_COLUMNS)
    if write_header:
        _writer.writeheader()
    _path = path
    return _writer


def enabled() -> bool:
    return bool(os.environ.get("THE_LAB_PERF_LOG"))


def log_request(
    *,
    method: str,
    path: str,
    normalized_path: str,
    status: int,
    duration_ms: float,
    source: str,
    query: str = "",
    body_bytes: int = 0,
    response_bytes: int = 0,
    client_ip: str = "",
) -> None:
    """Append one row to the perf CSV. No-op if THE_LAB_PERF_LOG is unset."""
    target = os.environ.get("THE_LAB_PERF_LOG")
    if not target:
        return
    row = {
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "method": method,
        "path": path,
        "normalized_path": normalized_path,
        "status": status,
        "duration_ms": f"{duration_ms:.2f}",
        "source": source,
        "query": query,
        "body_bytes": body_bytes,
        "response_bytes": response_bytes,
        "client_ip": client_ip,
    }
    with _lock:
        writer = _ensure_open(Path(target))
        writer.writerow(row)
