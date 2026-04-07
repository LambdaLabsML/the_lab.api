"""Lightweight API endpoint usage stats.

Tracks per-endpoint call counts and sequential call patterns (bigrams).
Stored in `.the_lab/api_stats.json` and flushed periodically.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from pathlib import Path


# Normalize paths like /api/v1/ideas/42 → /api/v1/ideas/{id}
_ID_RE = re.compile(r"/(\d+)(?=/|$)")


def normalize_path(path: str) -> str:
    """Replace numeric path segments with {id} for grouping."""
    return _ID_RE.sub("/{id}", path)


class ApiStats:
    """Thread-safe API call counter with pattern tracking."""

    def __init__(self, stats_path: Path):
        self._path = stats_path
        self._lock = threading.Lock()
        self._calls: dict[str, int] = defaultdict(int)  # "METHOD /path" → count
        self._patterns: dict[str, int] = defaultdict(int)  # "A → B" → count
        self._last_call: dict[str, str] = {}  # client_ip → last endpoint
        self._last_flush = time.monotonic()
        self._dirty = False
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._calls = defaultdict(int, data.get("calls", {}))
                self._patterns = defaultdict(int, data.get("patterns", {}))
            except (json.JSONDecodeError, OSError):
                pass

    def flush(self):
        """Write stats to disk."""
        with self._lock:
            if not self._dirty:
                return
            data = {
                "calls": dict(self._calls),
                "patterns": dict(self._patterns),
            }
            self._dirty = False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    def record(self, method: str, path: str, client_ip: str = ""):
        """Record an API call and update pattern tracking."""
        key = f"{method} {normalize_path(path)}"
        with self._lock:
            self._calls[key] += 1
            # Track sequential patterns (bigrams) per client
            prev = self._last_call.get(client_ip)
            if prev and prev != key:
                pattern = f"{prev} → {key}"
                self._patterns[pattern] += 1
            self._last_call[client_ip] = key
            self._dirty = True
        # Periodic flush (every 30s)
        if time.monotonic() - self._last_flush > 30:
            self._last_flush = time.monotonic()
            self.flush()

    def get_stats(self) -> dict:
        """Return current stats for the API response."""
        with self._lock:
            calls = dict(self._calls)
            patterns = dict(self._patterns)
        # Sort by count descending
        sorted_calls = sorted(calls.items(), key=lambda x: x[1], reverse=True)
        sorted_patterns = sorted(patterns.items(), key=lambda x: x[1], reverse=True)
        return {
            "calls": [{"endpoint": k, "count": v} for k, v in sorted_calls],
            "patterns": [{"sequence": k, "count": v} for k, v in sorted_patterns],
            "total_calls": sum(calls.values()),
        }

    def merge(self, calls: dict[str, int], patterns: dict[str, int]):
        """Merge external stats (e.g. from backfill) into current stats."""
        with self._lock:
            for k, v in calls.items():
                self._calls[k] += v
            for k, v in patterns.items():
                self._patterns[k] += v
            self._dirty = True
