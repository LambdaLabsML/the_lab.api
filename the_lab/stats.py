"""Lightweight API endpoint usage stats.

Tracks per-endpoint call counts and sequential call patterns (n-grams
of length 2–5). Stored in `.the_lab/api_stats.json` and flushed periodically.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

MAX_NGRAM = 5

# Normalize paths like /api/v1/ideas/42 or /experiments/408.11 → {id}
_ID_RE = re.compile(r"/(\d+(?:\.\d+)?)(?=/|$)")
# Normalize alphanumeric slug IDs after known collection endpoints
# e.g. /agents/mkn23, /messages/abc12 → /agents/{id}, /messages/{id}
# Excludes known literal sub-paths (register, log, tags, search, etc.)
_LITERAL_SEGMENTS = frozenset({
    "register", "log", "tags", "search", "pull", "start", "cancel",
    "rerun", "done", "slurm_done", "timeseries", "compare", "analyze",
    "read", "read_all", "batch", "rename", "new", "checkout", "conclude",
    "abandon", "adopt", "note", "notes", "suggest",
})
_SLUG_RE = re.compile(
    r"(/(agents|messages|experiments|ideas|tasks|runs|jobs))/([A-Za-z0-9][A-Za-z0-9_-]{1,40})(?=/|$)"
)


def normalize_path(path: str) -> str:
    """Replace numeric and alphanumeric ID segments with {id} for grouping."""
    path = _ID_RE.sub("/{id}", path)

    def _replace_slug(m: re.Match) -> str:
        if m.group(3) in _LITERAL_SEGMENTS:
            return m.group(0)  # keep as-is
        return m.group(1) + "/{id}"

    path = _SLUG_RE.sub(_replace_slug, path)
    return path


MAX_HISTORY = 200  # recent calls to keep in memory


class ApiStats:
    """Thread-safe API call counter with n-gram pattern tracking."""

    def __init__(self, stats_path: Path):
        self._path = stats_path
        self._lock = threading.Lock()
        self._calls: dict[str, int] = defaultdict(int)
        # patterns_by_n[n] = {"A → B → C": count}  for n=2..5
        self._patterns: dict[int, dict[str, int]] = {
            n: defaultdict(int) for n in range(2, MAX_NGRAM + 1)
        }
        # Per-endpoint response size tracking: total_bytes and call count
        # (for computing avg). Keyed by normalized path.
        self._resp_bytes_total: dict[str, int] = defaultdict(int)
        self._resp_bytes_count: dict[str, int] = defaultdict(int)
        self._resp_bytes_max: dict[str, int] = defaultdict(int)
        # Sliding window of recent calls per client for n-gram extraction
        self._history: dict[str, deque[str]] = {}
        # Recent call log (ring buffer)
        self._recent: deque[dict] = deque(maxlen=MAX_HISTORY)
        # Hourly per-endpoint buckets ("2026-07-02T14" -> {key: count}) so the
        # dashboard can filter statistics to a time period. Pruned to ~14 days.
        self._hourly: dict[str, dict[str, int]] = {}
        # Same idea for response sizes: bucket -> {key: [total_b, count, max_b]}.
        self._resp_hourly: dict[str, dict[str, list]] = {}
        self._last_flush = time.monotonic()
        self._dirty = False
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._calls = defaultdict(int, data.get("calls", {}))
                for n in range(2, MAX_NGRAM + 1):
                    stored = data.get(f"patterns_{n}", {})
                    if n == 2 and not stored:
                        stored = data.get("patterns", {})
                    self._patterns[n] = defaultdict(int, stored)
                self._resp_bytes_total = defaultdict(int, data.get("resp_bytes_total", {}))
                self._resp_bytes_count = defaultdict(int, data.get("resp_bytes_count", {}))
                self._resp_bytes_max   = defaultdict(int, data.get("resp_bytes_max", {}))
                self._hourly = {k: dict(v) for k, v in data.get("hourly", {}).items()
                                if isinstance(v, dict)}
                self._resp_hourly = {k: dict(v) for k, v in data.get("resp_hourly", {}).items()
                                     if isinstance(v, dict)}
            except (json.JSONDecodeError, OSError):
                pass

    @staticmethod
    def _hour_bucket() -> str:
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone.utc).isoformat()[:13]

    @staticmethod
    def _prune_buckets(buckets: dict, keep: int = 14 * 24) -> None:
        if len(buckets) > keep + 2:
            for old in sorted(buckets)[: len(buckets) - keep]:
                del buckets[old]

    def record_response_size(self, method: str, path: str, size_bytes: int) -> None:
        """Track response size for an endpoint (call after the full response is built)."""
        key = f"{method} {normalize_path(path)}"
        with self._lock:
            self._resp_bytes_total[key] += size_bytes
            self._resp_bytes_count[key] += 1
            if size_bytes > self._resp_bytes_max[key]:
                self._resp_bytes_max[key] = size_bytes
            # Hourly bucket for period filtering: [total_b, count, max_b].
            hb = self._resp_hourly.setdefault(self._hour_bucket(), {})
            cell = hb.setdefault(key, [0, 0, 0])
            cell[0] += size_bytes
            cell[1] += 1
            if size_bytes > cell[2]:
                cell[2] = size_bytes
            self._prune_buckets(self._resp_hourly)
            self._dirty = True

    def flush(self):
        """Write stats to disk."""
        with self._lock:
            if not self._dirty:
                return
            data: dict = {"calls": dict(self._calls)}
            for n in range(2, MAX_NGRAM + 1):
                data[f"patterns_{n}"] = dict(self._patterns[n])
            data["resp_bytes_total"] = dict(self._resp_bytes_total)
            data["resp_bytes_count"] = dict(self._resp_bytes_count)
            data["resp_bytes_max"]   = dict(self._resp_bytes_max)
            data["hourly"] = {k: dict(v) for k, v in self._hourly.items()}
            data["resp_hourly"] = {k: dict(v) for k, v in self._resp_hourly.items()}
            self._dirty = False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    def record(self, method: str, path: str, client_ip: str = "",
               query: str = "", body_preview: str = "", status_code: int = 0,
               mcp: bool = False):
        """Record an API call and update n-gram pattern tracking."""
        key = f"{method} {normalize_path(path)}"
        import datetime as _dt
        entry = {
            "t": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "method": method,
            "path": path,
            "query": query,
            "body": body_preview[:200] if body_preview else "",
            "status": status_code,
        }
        if mcp:
            entry["mcp"] = True
        self._recent.append(entry)
        with self._lock:
            self._calls[key] += 1
            # Hourly bucket for period filtering (pruned to ~14 days).
            bucket = entry["t"][:13]
            hb = self._hourly.setdefault(bucket, {})
            hb[key] = hb.get(key, 0) + 1
            self._prune_buckets(self._hourly)
            # Maintain sliding window per client
            if client_ip not in self._history:
                self._history[client_ip] = deque(maxlen=MAX_NGRAM)
            hist = self._history[client_ip]
            hist.append(key)
            # Extract all n-grams from the window
            items = list(hist)
            for n in range(2, min(len(items), MAX_NGRAM) + 1):
                ngram = items[-n:]
                # Skip if all calls are the same endpoint (polling noise)
                if len(set(ngram)) == 1:
                    continue
                pattern = " → ".join(ngram)
                self._patterns[n][pattern] += 1
            self._dirty = True
        # Periodic flush (every 30s)
        if time.monotonic() - self._last_flush > 30:
            self._last_flush = time.monotonic()
            self.flush()

    def get_stats(self, pattern_length: int = 2, since_hours: float | None = None) -> dict:
        """Return current stats for the API response.

        ``since_hours`` filters the per-endpoint call counts AND the response
        sizes to the last N hours using the hourly buckets (data exists from
        the moment bucketing was deployed — older traffic only lives in the
        all-time aggregates). Patterns remain all-time; the response flags that.
        """
        n = max(2, min(pattern_length, MAX_NGRAM))
        with self._lock:
            if since_hours is not None and since_hours > 0:
                import datetime as _dt
                cutoff = (_dt.datetime.now(_dt.timezone.utc)
                          - _dt.timedelta(hours=since_hours)).isoformat()[:13]
                calls: dict[str, int] = {}
                for bucket, per_key in self._hourly.items():
                    if bucket >= cutoff:
                        for k, v in per_key.items():
                            calls[k] = calls.get(k, 0) + v
                # Response sizes from the same window.
                rb_total: dict[str, int] = {}
                rb_count: dict[str, int] = {}
                rb_max: dict[str, int] = {}
                for bucket, per_key in self._resp_hourly.items():
                    if bucket >= cutoff:
                        for k, cell in per_key.items():
                            rb_total[k] = rb_total.get(k, 0) + cell[0]
                            rb_count[k] = rb_count.get(k, 0) + cell[1]
                            if cell[2] > rb_max.get(k, 0):
                                rb_max[k] = cell[2]
            else:
                calls = dict(self._calls)
                rb_total = dict(self._resp_bytes_total)
                rb_count = dict(self._resp_bytes_count)
                rb_max = dict(self._resp_bytes_max)
            patterns = dict(self._patterns.get(n, {}))
        sorted_calls = sorted(calls.items(), key=lambda x: x[1], reverse=True)
        sorted_patterns = sorted(patterns.items(), key=lambda x: x[1], reverse=True)
        total = sum(calls.values())
        mcp_calls = sum(1 for r in self._recent if r.get("mcp"))

        # Build response-size table — sort by total bytes descending
        size_rows = []
        for key in set(rb_total) | set(calls):
            total_b = rb_total.get(key, 0)
            count   = rb_count.get(key, 0)
            size_rows.append({
                "endpoint":    key,
                "calls":       calls.get(key, 0),
                "total_kb":    round(total_b / 1024, 1),
                "avg_kb":      round(total_b / max(count, 1) / 1024, 1),
                "max_kb":      round(rb_max.get(key, 0) / 1024, 1),
            })
        size_rows.sort(key=lambda r: r["total_kb"], reverse=True)

        result = {
            "calls": [{"endpoint": k, "count": v} for k, v in sorted_calls],
            "patterns": [{"sequence": k, "count": v} for k, v in sorted_patterns],
            "response_sizes": size_rows,
            "pattern_length": n,
            "total_calls": total,
            "mcp_calls": mcp_calls,
            "curl_calls": total - mcp_calls,
            "period_hours": since_hours,
            # patterns are all-time aggregates (not bucketed); call counts and
            # response sizes honor since_hours via the hourly buckets.
            "patterns_scope": "all-time",
        }
        return result

    def get_history(self, limit: int = 50) -> list[dict]:
        """Return the most recent API calls (newest first)."""
        return list(reversed(self._recent))[:limit]

    def merge(self, calls: dict[str, int], patterns: dict[str, int],
              patterns_by_n: dict[int, dict[str, int]] | None = None,
              reset: bool = False):
        """Merge external stats (e.g. from backfill) into current stats.

        If reset=True, replace all existing stats instead of merging.
        """
        with self._lock:
            if reset:
                self._calls.clear()
                for n in self._patterns:
                    self._patterns[n].clear()
            for k, v in calls.items():
                self._calls[k] += v
            # Legacy: flat patterns dict is treated as bigrams
            if patterns:
                for k, v in patterns.items():
                    self._patterns[2][k] += v
            # New: per-length patterns
            if patterns_by_n:
                for n_str, pats in patterns_by_n.items():
                    n = int(n_str)
                    if 2 <= n <= MAX_NGRAM:
                        for k, v in pats.items():
                            self._patterns[n][k] += v
            self._dirty = True
