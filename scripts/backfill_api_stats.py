#!/usr/bin/env python3
"""Backfill API stats from Claude and Codex conversation histories.

Scans JSONL session files for curl/fetch calls to the lab API and extracts
endpoint paths. Builds call counts and sequential patterns (bigrams), then
merges them into the existing .the_lab/api_stats.json.

Usage:
    python scripts/backfill_api_stats.py [--repo-dir ./example_proj]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Add parent dir to path so we can import the stats module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from the_lab.stats import ApiStats, normalize_path


# Patterns to extract API calls from shell commands and text
_API_RE = re.compile(
    r"""(?:localhost:800[012]|127\.0\.0\.1:800[012])"""
    r"""(/api/v1/[a-zA-Z0-9_/{}\-]+)"""
)
# HTTP method from curl command
_METHOD_RE = re.compile(r"""-X\s+(GET|POST|PUT|DELETE|PATCH)""", re.IGNORECASE)


def extract_api_calls_from_text(text: str) -> list[tuple[str, str]]:
    """Extract (method, path) pairs from a text containing curl/fetch commands."""
    results = []
    urls = _API_RE.findall(text)
    if not urls:
        return results
    # Try to detect HTTP method
    method_match = _METHOD_RE.search(text)
    default_method = method_match.group(1).upper() if method_match else None
    if default_method is None:
        # POST if -d/--data present, otherwise GET
        default_method = "POST" if ("-d " in text or "--data" in text or "-d'" in text) else "GET"
    for url in urls:
        # Strip query params and trailing punctuation for normalization
        path = url.split("?")[0].rstrip(")'\"\\,;")
        results.append((default_method, path))
    return results


def scan_claude_sessions(project_dir: Path) -> list[tuple[str, str]]:
    """Extract API calls from Claude project JSONL files."""
    calls: list[tuple[str, str]] = []
    jsonl_files = sorted(project_dir.glob("*.jsonl"))
    print(f"  Claude: {len(jsonl_files)} session files")
    for f in jsonl_files:
        try:
            for line in f.read_text().splitlines():
                if "api/v1" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = obj.get("message") if isinstance(obj, dict) else None
                if not isinstance(msg, dict):
                    continue
                for content in msg.get("content", []):
                    if content.get("type") != "tool_use":
                        continue
                    inp = content.get("input", {})
                    # Bash commands
                    cmd = inp.get("command", "")
                    if cmd:
                        calls.extend(extract_api_calls_from_text(cmd))
                    # Also check tool results that might reference API
                    result = content.get("result", "")
                    if isinstance(result, str) and "api/v1" in result:
                        calls.extend(extract_api_calls_from_text(result))
        except Exception as e:
            print(f"    warn: {f.name}: {e}")
    return calls


def scan_codex_sessions(sessions_dir: Path) -> list[tuple[str, str]]:
    """Extract API calls from Codex session JSONL files."""
    calls: list[tuple[str, str]] = []
    jsonl_files = sorted(sessions_dir.rglob("*.jsonl"))
    print(f"  Codex: {len(jsonl_files)} session files")
    for f in jsonl_files:
        try:
            for line in f.read_text().splitlines():
                if "api/v1" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Codex stores tool calls in response_item payloads
                payload = obj.get("payload", {})
                if isinstance(payload, dict):
                    for content in payload.get("content", []):
                        text = content.get("text", "")
                        if "api/v1" in text:
                            calls.extend(extract_api_calls_from_text(text))
                    # Also check arguments field
                    args = payload.get("arguments", "")
                    if isinstance(args, str) and "api/v1" in args:
                        calls.extend(extract_api_calls_from_text(args))
                # Raw text scan as fallback
                raw = json.dumps(payload) if isinstance(payload, dict) else str(payload)
                if "api/v1" in raw:
                    for match in extract_api_calls_from_text(raw):
                        if match not in calls[-5:]:  # basic dedup
                            calls.append(match)
        except Exception as e:
            print(f"    warn: {f.name}: {e}")
    return calls


def build_stats(
    calls: list[tuple[str, str]],
) -> tuple[dict[str, int], dict[int, dict[str, int]]]:
    """Build call counts and n-gram patterns (2-5) from a sequence of (method, path) calls."""
    counts: dict[str, int] = defaultdict(int)
    patterns_by_n: dict[int, dict[str, int]] = {n: defaultdict(int) for n in range(2, 6)}
    window: list[str] = []
    for method, path in calls:
        key = f"{method} {normalize_path(path)}"
        counts[key] += 1
        window.append(key)
        if len(window) > 5:
            window.pop(0)
        for n in range(2, min(len(window), 5) + 1):
            ngram = window[-n:]
            if len(set(ngram)) == 1:
                continue
            patterns_by_n[n][" → ".join(ngram)] += 1
    return dict(counts), {n: dict(p) for n, p in patterns_by_n.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default="./example_proj", help="Repo dir with .the_lab/")
    args = parser.parse_args()

    repo = Path(args.repo_dir).resolve()
    stats_path = repo / ".the_lab" / "api_stats.json"

    print("Scanning conversation histories...")
    all_calls: list[tuple[str, str]] = []

    # Claude sessions
    claude_dirs = [
        Path.home() / ".claude" / "projects" / "-lambda-nfs-architects-us-south-2-matholympiad-train",
        Path.home() / ".claude" / "projects" / "-lambda-nfs-architects-us-south-2",
    ]
    for d in claude_dirs:
        if d.exists():
            calls = scan_claude_sessions(d)
            print(f"    found {len(calls)} API calls")
            all_calls.extend(calls)

    # Codex sessions
    codex_dir = Path.home() / ".codex" / "sessions"
    if codex_dir.exists():
        calls = scan_codex_sessions(codex_dir)
        print(f"    found {len(calls)} API calls")
        all_calls.extend(calls)

    if not all_calls:
        print("No API calls found in conversation histories.")
        return

    counts, patterns_by_n = build_stats(all_calls)

    print(f"\nTotal calls extracted: {len(all_calls)}")
    print(f"Unique endpoints: {len(counts)}")
    for n in range(2, 6):
        print(f"Unique {n}-grams: {len(patterns_by_n.get(n, {}))}")

    print("\nTop endpoints:")
    for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {v:4d}  {k}")

    print("\nTop 2-step patterns:")
    for k, v in sorted(patterns_by_n.get(2, {}).items(), key=lambda x: x[1], reverse=True)[:5]:
        print(f"  {v:4d}  {k}")

    print("\nTop 3-step patterns:")
    for k, v in sorted(patterns_by_n.get(3, {}).items(), key=lambda x: x[1], reverse=True)[:5]:
        print(f"  {v:4d}  {k}")

    # Merge into existing stats file
    stats = ApiStats(stats_path)
    stats.merge(counts, {}, patterns_by_n=patterns_by_n)
    stats.flush()
    print(f"\nMerged into {stats_path}")

    # Also push to running server if available
    import urllib.request
    payload = json.dumps({"calls": counts, "patterns_by_n": patterns_by_n}).encode()
    for port in (8001, 8002):
        try:
            req = urllib.request.Request(
                f"http://localhost:{port}/api/v1/stats/import",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            result = json.loads(resp.read())
            print(f"Pushed to localhost:{port} — total_calls: {result['total_calls']}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
