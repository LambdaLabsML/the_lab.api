#!/usr/bin/env python3 -u
"""Evaluate an API change by running an agent against the test project.

Starts a Lab instance from the current branch's code, copies the test_project
fixture to a temp dir, launches an inner agent, and collects efficiency metrics.

Usage (from experiment scripts):
    python .the_lab/artifacts/run_eval.py --model haiku --budget 15

Metrics output (Lab-compatible JSON on stdout):
    - api_score: composite (higher = better API)
    - experiments_till_quality: how many experiments to reach threshold
    - tokens breakdown: bash, context, reasoning, read_search, edit_write
    - cost breakdown: per category
    - api_calls, calls_per_idea, wall_time_s, etc.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.request

# Force unbuffered output — critical when stdout/stderr are redirected to log files
os.environ["PYTHONUNBUFFERED"] = "1"
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
import time
from pathlib import Path

# resolve() follows symlinks — gives us the real location of static fixtures
SCRIPT_DIR = Path(__file__).resolve().parent
TEST_PROJECT_SRC = SCRIPT_DIR / "test_project"

# REPO_ROOT = the working directory where the experiment runs.
# In a worktree, this is the branch's code; in the main checkout, it's the repo root.
# The start_lab() function uses this to find the_lab/ module to import.
REPO_ROOT = Path(os.environ.get("THE_LAB_REPO", os.getcwd())).resolve()

# Opus 4 pricing (per million tokens)
def _get_host() -> str:
    """Best-effort external hostname/IP for clickable URLs."""
    import socket
    try:
        # Connect to a public DNS to find our outward-facing IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostname()


def _lab_get(url: str, timeout: float = 5):
    """GET a Lab API URL with the dashboard header so it's excluded from stats."""
    req = urllib.request.Request(url, headers={"X-The-Lab-Source": "dashboard"})
    return urllib.request.urlopen(req, timeout=timeout)


PRICE_INPUT = 15.0
PRICE_OUTPUT = 75.0
PRICE_CACHE_WRITE = 18.75
PRICE_CACHE_READ = 1.50


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="haiku", help="Model for inner agent (haiku|sonnet|opus)")
    p.add_argument("--budget", type=int, default=15, help="Max experiments before giving up")
    p.add_argument("--timeout", type=int, default=900, help="Wall-time cap in seconds")
    p.add_argument("--max-cost", type=float, default=0, help="Max dollar cost for inner agent (0 = no limit)")
    p.add_argument("--baseline", default=str(SCRIPT_DIR / "baseline.json"),
                   help="Path to baseline.json for score normalization")
    p.add_argument("--output", default=None,
                   help="Write metrics JSON to this file (default: stdout)")
    return p.parse_args()


def copy_test_project(dest: Path):
    """Copy the test_project fixture to dest, append API instructions, init git."""
    shutil.copytree(TEST_PROJECT_SRC, dest, dirs_exist_ok=True)

    # Concatenate PROMPT.md (fixture) + PROMPT_append.md (from the branch)
    # The append file contains API workflow instructions that the optimization
    # agent can modify alongside API code changes.
    prompt_append = REPO_ROOT / "PROMPT_append.md"
    if prompt_append.exists():
        prompt_dst = dest / "PROMPT.md"
        with open(prompt_dst, "a") as f:
            f.write("\n")
            f.write(prompt_append.read_text())
        print(f"  Appended PROMPT_append.md to test project PROMPT.md", file=sys.stderr)

    # The Lab needs a git repo — init one in the copy
    subprocess.run(["git", "init"], cwd=str(dest), capture_output=True)
    subprocess.run(["git", "config", "user.name", "eval"], cwd=str(dest), capture_output=True)
    subprocess.run(["git", "config", "user.email", "eval@local"], cwd=str(dest), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(dest), capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(dest), capture_output=True)


def build_dashboard_async(repo_root: Path):
    """Copy dashboard sources and run npm build in background."""
    dashboard_src = repo_root / "dashboard"
    if not dashboard_src.exists():
        # Try parent repo
        dashboard_src = repo_root.parent / "dashboard"
    if not (dashboard_src / "package.json").exists():
        return

    static_dir = repo_root / "the_lab" / "static"
    if static_dir.exists() and (static_dir / "index.html").exists():
        return  # already built

    def _build():
        try:
            # npm install + build
            subprocess.run(["npm", "install", "--no-audit", "--no-fund"],
                           cwd=str(dashboard_src), capture_output=True, timeout=60)
            subprocess.run(["npx", "vite", "build"],
                           cwd=str(dashboard_src), capture_output=True, timeout=60)
            # Copy built output to the_lab/static/
            built = dashboard_src.parent / "the_lab" / "static"
            if built.exists():
                if static_dir.exists():
                    shutil.rmtree(static_dir)
                shutil.copytree(built, static_dir)
                print("  Dashboard built.", file=sys.stderr)
        except Exception as e:
            print(f"  Dashboard build skipped: {e}", file=sys.stderr)

    import threading
    threading.Thread(target=_build, daemon=True).start()


def start_lab(repo_dir: str, port: int) -> subprocess.Popen:
    """Start a Lab instance from the current branch's code (no sandbox)."""
    # Disable sandbox for the inner Lab — experiments need localhost access
    sandbox_conf = Path(repo_dir) / ".the_lab" / "sandbox" / "config.json"
    sandbox_conf.parent.mkdir(parents=True, exist_ok=True)
    sandbox_conf.write_text(json.dumps({"enabled": False}) + "\n")

    # Copy dashboard build so the inner Lab has a UI
    static_dst = REPO_ROOT / "the_lab" / "static"
    if not (static_dst / "index.html").exists():
        # Look for it in the parent repo
        parent_static = REPO_ROOT.parent / "the_lab" / "static"
        if not (parent_static / "index.html").exists():
            # Try two levels up (optimization/proj → optimization → the_lab.api)
            parent_static = REPO_ROOT.parent.parent / "the_lab" / "static"
        if (parent_static / "index.html").exists():
            static_dst.mkdir(parents=True, exist_ok=True)
            shutil.copytree(parent_static, static_dst, dirs_exist_ok=True)

    env = {**os.environ, "THE_LAB_REPO": repo_dir}
    # Strip any inherited sandbox env vars
    for key in list(env):
        if "SANDBOX" in key:
            del env[key]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "the_lab.app:app",
         "--host", "0.0.0.0", "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for it to be ready (up to 30s)
    import urllib.request
    host = _get_host()
    print(f"  Waiting for Lab on {host}:{port} (cwd={proc.args[0] if hasattr(proc, 'args') else '?'})...", file=sys.stderr)
    for i in range(60):
        rc = proc.poll()
        if rc is not None:
            raise RuntimeError(f"Lab crashed on startup (exit code {rc})")
        try:
            _lab_get(f"http://{host}:{port}/api/v1/backlog", timeout=1)
            break
        except Exception as e:
            if i % 10 == 9:
                print(f"  Still waiting ({i+1}/60): {e}", file=sys.stderr)
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError(f"Lab failed to start on http://{host}:{port} after 30s. "
                           f"Verify the_lab/ code is valid and the port is accessible.")

    # Health check: verify API endpoints respond (no persistent state created)
    print("  Health check...", file=sys.stderr)
    base_url = f"http://{_get_host()}:{port}/api/v1"
    try:
        checks = [
            ("backlog", "GET", f"{base_url}/backlog"),
            ("ideas", "GET", f"{base_url}/ideas"),
            ("docs", "GET", f"http://{_get_host()}:{port}/docs"),
        ]
        for name, method, url in checks:
            resp = _lab_get(url, timeout=5)
            if resp.status != 200:
                print(f"  Health check warning: {name} returned {resp.status}", file=sys.stderr)

        print("  Health check passed: API responding", file=sys.stderr)
    except Exception as e:
        print(f"  Health check FAILED: {e}", file=sys.stderr)
        print("  The inner agent may not be able to use the API.", file=sys.stderr)

    return proc


def launch_agent(
    prompt_path: str, api_port: int, model: str, timeout: int, budget: int,
    **kwargs,
) -> None:
    """Launch an inner agent, stop when budget experiments are completed or timeout."""
    # Session JSONL is found post-hoc via find_session_jsonl(project_dir)

    instruction = (
        f"You have access to a local experiment management API at http://localhost:{api_port}/api/v1. "
        f"Read PROMPT.md for your task and the API workflow. "
        f"Follow the workflow: create idea, checkout, edit benchmark/kernels.py, "
        f"create experiment with script_content='#!/bin/bash\\nset -euo pipefail\\npython benchmark/eval_harness.py', "
        f"start it, wait, check results. Iterate to maximize the composite score. "
        f"Branch from successful ideas for further improvements."
    )

    env = {**os.environ}

    # Use --print with stream-json for live progress visibility.
    max_cost = kwargs.get("max_cost", 0)
    cmd = [
        "claude", "--dangerously-skip-permissions",
        "--model", model,
        "--print", "--verbose", "--output-format", "stream-json",
    ]
    if max_cost > 0:
        cmd.extend(["--max-budget-usd", str(max_cost)])
    cmd.extend(["-p", instruction])

    # Stream stdout and parse JSON events for human-readable progress
    proc = subprocess.Popen(
        cmd, cwd=prompt_path, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    def _stream_progress():
        """Read stream-json events and print a compact progress summary."""
        for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = evt.get("type", "")
            if t == "assistant":
                msg = evt.get("message", {})
                for c in msg.get("content", []):
                    if c.get("type") == "tool_use":
                        name = c.get("name", "?")
                        inp = c.get("input", {})
                        preview = ""
                        if name == "Bash":
                            preview = (inp.get("command") or "")[:80]
                        elif name in ("Read", "Write", "Edit"):
                            preview = (inp.get("file_path") or "")
                        elif name in ("Grep", "Glob"):
                            preview = (inp.get("pattern") or "")
                        print(f"  [{name}] {preview}", file=sys.stderr)
                    elif c.get("type") == "text":
                        text = c.get("text", "")
                        if text.strip():
                            short = text.strip()[:120]
                            print(f"  > {short}", file=sys.stderr)
            elif t == "user":
                # Tool results — show capped output
                msg = evt.get("message", {})
                for c in msg.get("content", []):
                    content = c.get("content", "")
                    if isinstance(content, str) and content.strip():
                        # Cap to first 3 lines, 200 chars
                        lines = content.strip().split("\n")
                        preview = "\n".join(lines[:3])
                        if len(lines) > 3:
                            preview += f"\n    ... ({len(lines)} lines)"
                        if len(preview) > 200:
                            preview = preview[:200] + "..."
                        print(f"    ← {preview}", file=sys.stderr)
            elif t == "result":
                cost = evt.get("total_cost_usd", 0)
                turns = evt.get("num_turns", 0)
                print(f"  [done] {turns} turns, ${cost:.3f}", file=sys.stderr)

    import threading
    stream_thread = threading.Thread(target=_stream_progress, daemon=True)
    stream_thread.start()

    # Poll the inner Lab for experiment count — kill agent when budget reached
    # Write progress to $THE_LAB_PROGRESS if set (for the outer Lab's UI)
    import urllib.request
    progress_file = os.environ.get("THE_LAB_PROGRESS")
    start = time.time()
    deadline = start + timeout

    # Write initial progress immediately
    if progress_file:
        Path(progress_file).write_text(json.dumps({
            "pct_complete": 0, "experiments_completed": 0,
            "experiments_running": 0, "budget": budget, "elapsed_s": 0,
            "status": "inner agent running",
        }))

    while proc.poll() is None and time.time() < deadline:
        time.sleep(5)
        elapsed = time.time() - start
        try:
            resp = _lab_get(f"http://{_get_host()}:{api_port}/api/v1/chart-data", timeout=2)
            data = json.loads(resp.read())
            n_completed = len(data.get("experiments", []))
            n_running = len(data.get("running", []))
        except Exception:
            n_completed = 0
            n_running = 0

        # Extract best score and details from completed experiments
        best_score = 0.0
        best_exp_id = None
        best_exp_label = None
        best_metrics = {}
        completed_exps = data.get("experiments", []) if isinstance(data, dict) else []
        for exp in completed_exps:
            m = exp.get("metrics") or {}
            s = m.get("score", 0)
            if s > best_score:
                best_score = s
                best_exp_id = exp.get("id")
                best_exp_label = exp.get("label", str(exp.get("id", "?")))
                best_metrics = m

        # Count ideas from the Lab
        try:
            ideas_resp = _lab_get(f"http://{_get_host()}:{api_port}/api/v1/ideas", timeout=2)
            ideas_data = json.loads(ideas_resp.read())
            n_ideas = len(ideas_data)
            n_concluded = sum(1 for i in ideas_data if i.get("status") == "concluded")
            n_abandoned = sum(1 for i in ideas_data if i.get("status") == "abandoned")
        except Exception:
            n_ideas = 0
            n_concluded = 0
            n_abandoned = 0

        pct = max(n_completed / max(budget, 1), elapsed / max(timeout, 1))
        pct = min(pct, 0.99)

        # Log progress to terminal
        if n_completed > 0 or n_running > 0:
            print(f"  [{int(elapsed)}s] experiments: {n_completed}/{budget} done, "
                  f"{n_running} running, {n_ideas} ideas | best score: {best_score:.4f}"
                  f"{f' (exp/{best_exp_label})' if best_exp_label else ''}",
                  file=sys.stderr)

        if progress_file:
            progress = {
                "pct_complete": round(pct * 100, 1),
                "status": "inner agent running",
                "elapsed_s": round(elapsed, 0),
                "budget": budget,
                "experiments_completed": n_completed,
                "experiments_running": n_running,
                "ideas_total": n_ideas,
                "ideas_concluded": n_concluded,
                "ideas_abandoned": n_abandoned,
                "best_score": round(best_score, 6),
                "best_experiment_id": best_exp_id,
                "best_experiment_label": best_exp_label,
            }
            # Include per-kernel breakdown from best experiment
            if best_metrics:
                for k, v in best_metrics.items():
                    if k.endswith("_accuracy") or k.endswith("_ns") or k.startswith("memory"):
                        progress[k] = v
            Path(progress_file).write_text(json.dumps(progress))

        if n_completed >= budget:
            print(f"Budget reached ({n_completed}/{budget} experiments). Stopping agent.",
                  file=sys.stderr)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            break

    if proc.poll() is None:
        print("Timeout reached. Killing agent.", file=sys.stderr)
        proc.kill()
        proc.wait()

    return


def collect_api_stats(port: int) -> dict:
    """Fetch stats from the test Lab instance."""
    import urllib.request
    try:
        resp = _lab_get(f"http://{_get_host()}:{port}/api/v1/stats?pattern_length=2", timeout=5)
        return json.loads(resp.read())
    except Exception:
        return {"total_calls": 0, "calls": [], "patterns": []}


def collect_lab_state(port: int) -> dict:
    """Fetch ideas and experiments from the test Lab instance."""
    import urllib.request
    try:
        ideas = json.loads(_lab_get(
            f"http://{_get_host()}:{port}/api/v1/ideas", timeout=5).read())
    except Exception:
        ideas = []

    experiments = []
    for idea in ideas:
        try:
            exps = json.loads(_lab_get(
                f"http://{_get_host()}:{port}/api/v1/ideas/{idea['id']}/experiments", timeout=5).read())
            experiments.extend(exps)
        except Exception:
            pass

    # Fetch idea DAG
    try:
        graph = json.loads(_lab_get(
            f"http://{_get_host()}:{port}/api/v1/graph", timeout=5).read())
    except Exception:
        graph = {"nodes": [], "edges": []}

    return {"ideas": ideas, "experiments": experiments, "graph": graph}


def find_session_jsonl(project_dir: Path, start_time: float) -> list[Path]:
    """Find Claude session JSONL files for the inner agent's project dir.

    Claude stores sessions under ~/.claude/projects/<mangled-path>/.
    The mangled path replaces all non-alphanumeric chars with - and prepends -.
    Only returns files modified after start_time (to avoid stale sessions).
    """
    import re
    resolved = str(project_dir.resolve()).lstrip("/")
    mangled = "-" + re.sub(r"[^a-zA-Z0-9]", "-", resolved)
    session_dir = Path.home() / ".claude" / "projects" / mangled
    files = []
    if session_dir.exists():
        for f in sorted(session_dir.glob("*.jsonl")):
            if f.stat().st_mtime >= start_time:
                files.append(f)
    if files:
        print(f"  Found {len(files)} session file(s) in {session_dir.name}", file=sys.stderr)
    else:
        print(f"  Warning: no session JSONL found in {mangled}", file=sys.stderr)
    return files


def parse_session_tokens(project_dir: Path, start_time: float = 0) -> dict:
    """Parse inner agent's session JSONL for token breakdown."""
    categories = {
        "bash": {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "calls": 0},
        "context": {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "calls": 0},
        "reasoning": {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "calls": 0},
        "read_search": {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "calls": 0},
        "edit_write": {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "calls": 0},
    }

    session_files = find_session_jsonl(project_dir, start_time)

    for f in session_files:
        for line in open(f):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not usage:
                continue

            content = msg.get("content", [])
            has_tool = any(c.get("type") == "tool_use" for c in content if isinstance(c, dict))

            if has_tool:
                tool_names = [c.get("name", "?") for c in content
                              if isinstance(c, dict) and c.get("type") == "tool_use"]
                if any(n in ("Read", "Grep", "Glob") for n in tool_names):
                    cat = "read_search"
                elif any(n in ("Edit", "Write") for n in tool_names):
                    cat = "edit_write"
                elif "Bash" in tool_names:
                    cat = "bash"
                else:
                    cat = "bash"  # default tool calls to bash
            else:
                stop = msg.get("stop_reason", "none")
                cat = "reasoning" if stop == "end_turn" else "context"

            categories[cat]["calls"] += 1
            categories[cat]["input"] += usage.get("input_tokens", 0)
            categories[cat]["output"] += usage.get("output_tokens", 0)
            categories[cat]["cache_create"] += usage.get("cache_creation_input_tokens", 0)
            categories[cat]["cache_read"] += usage.get("cache_read_input_tokens", 0)

    # Compute costs and call counts
    result = {}
    total_tokens = 0
    total_cost = 0.0
    total_calls = 0
    for cat, u in categories.items():
        tokens = u["input"] + u["output"] + u["cache_create"] + u["cache_read"]
        cost = (u["input"] * PRICE_INPUT + u["output"] * PRICE_OUTPUT +
                u["cache_create"] * PRICE_CACHE_WRITE + u["cache_read"] * PRICE_CACHE_READ) / 1e6
        result[f"tokens_{cat}"] = tokens
        result[f"cost_{cat}"] = round(cost, 4)
        result[f"calls_{cat}"] = u["calls"]
        total_tokens += tokens
        total_cost += cost
        total_calls += u["calls"]

    result["tokens_total"] = total_tokens
    result["cost_total"] = round(total_cost, 4)
    result["calls_total"] = total_calls
    return result


def analyze_idea_dag(graph: dict, ideas: list[dict]) -> dict:
    """Analyze the idea DAG for branching behavior."""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    # Build parent map
    children: dict[int, list[int]] = {}
    parents: dict[int, list[int]] = {}
    for e in edges:
        children.setdefault(e["from"], []).append(e["to"])
        parents.setdefault(e["to"], []).append(e["from"])

    # Compute depth (longest path from root)
    depth_map: dict[int, int] = {}
    def get_depth(nid: int) -> int:
        if nid in depth_map:
            return depth_map[nid]
        pars = parents.get(nid, [])
        d = max((get_depth(p) for p in pars), default=-1) + 1
        depth_map[nid] = d
        return d
    for n in nodes:
        get_depth(n["id"])

    max_depth = max(depth_map.values()) if depth_map else 0
    ideas_with_parents = sum(1 for n in nodes if parents.get(n["id"]))

    status_counts = {}
    for idea in ideas:
        s = idea.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    return {
        "dag_max_depth": max_depth,
        "dag_ideas_with_parents": ideas_with_parents,
        "dag_ideas_abandoned": status_counts.get("abandoned", 0),
        "dag_ideas_concluded": status_counts.get("concluded", 0),
        "dag_ideas_active": status_counts.get("active", 0),
        "dag_branching_ratio": round(ideas_with_parents / max(len(nodes), 1), 3),
    }


def build_per_experiment_stats(
    experiments: list[dict], history: list[dict],
) -> list[dict]:
    """Slice the API call history into per-experiment windows.

    Each experiment gets the API calls between its created_at and finished_at
    (or the next experiment's created_at).
    """
    if not experiments or not history:
        return []

    # Sort experiments by creation time
    sorted_exps = sorted(experiments, key=lambda e: e.get("created_at", ""))

    # Build time windows: each experiment owns calls from its created_at
    # until the next experiment's created_at (or end of history)
    windows = []
    for i, exp in enumerate(sorted_exps):
        start = exp.get("created_at", "")
        if i + 1 < len(sorted_exps):
            end = sorted_exps[i + 1].get("created_at", "")
        else:
            end = "9999"  # capture everything after last experiment

        calls_in_window = [
            h for h in history
            if start <= h.get("t", "") < end
        ]

        # Count calls by endpoint
        endpoint_counts: dict[str, int] = {}
        for h in calls_in_window:
            key = f"{h['method']} {h['path']}"
            endpoint_counts[key] = endpoint_counts.get(key, 0) + 1

        windows.append({
            "experiment_id": exp.get("id"),
            "idea_id": exp.get("idea_id"),
            "status": exp.get("status"),
            "score": (exp.get("metrics") or {}).get("score"),
            "api_calls": len(calls_in_window),
            "top_endpoints": sorted(
                [{"endpoint": k, "count": v} for k, v in endpoint_counts.items()],
                key=lambda x: -x["count"],
            )[:5],
        })

    return windows


def compute_confusion_metrics(history: list[dict]) -> dict:
    """Detect agent confusion from API call history patterns.

    Confusion signals:
    - retry_rate: consecutive identical endpoint calls (agent retrying)
    - error_rate: 4xx/5xx responses (wrong params/endpoint)
    - correction_rate: list→detail escalations (list response was insufficient)
    - abandon_rate: create→cancel/abandon patterns (immediate regret)
    - oscillation_rate: A→B→A patterns (back-and-forth indecision)
    """
    if len(history) < 2:
        return {
            "confusion_score": 0.0,
            "retry_rate": 0.0,
            "error_rate": 0.0,
            "correction_rate": 0.0,
            "abandon_rate": 0.0,
            "oscillation_rate": 0.0,
            "confusion_examples": [],
        }

    # Inline _normalize to avoid dependency on the_lab module
    import re
    _id_re = re.compile(r"/(\d+)(?=/|$)")
    def _normalize(path: str) -> str:
        return _id_re.sub("/{id}", path)

    total = len(history)
    retries = 0
    errors = 0
    corrections = 0
    abandons = 0
    oscillations = 0
    examples: list[dict] = []

    for i in range(total):
        h = history[i]
        key = f"{h['method']} {_normalize(h['path'])}"
        status = h.get("status", 200)

        # Error responses
        if status >= 400:
            errors += 1
            if len(examples) < 10:
                examples.append({"type": "error", "call": key, "status": status, "index": i})

        if i == 0:
            continue
        prev = history[i - 1]
        prev_key = f"{prev['method']} {_normalize(prev['path'])}"

        # Retries: same endpoint called consecutively
        if key == prev_key:
            retries += 1
            if len(examples) < 10:
                examples.append({"type": "retry", "call": key, "index": i})

        # Corrections: GET list → GET detail of same resource
        # e.g., GET /ideas → GET /ideas/{id}
        if (h["method"] == "GET" and prev["method"] == "GET"
                and "/{id}" in _normalize(h["path"])
                and _normalize(h["path"]).replace("/{id}", "") in _normalize(prev["path"])):
            corrections += 1
            if len(examples) < 10:
                examples.append({"type": "correction", "from": prev_key, "to": key, "index": i})

        # Abandons: POST create → POST cancel/abandon
        if (prev["method"] == "POST" and h["method"] == "POST"
                and any(w in h["path"] for w in ("cancel", "abandon"))):
            abandons += 1
            if len(examples) < 10:
                examples.append({"type": "abandon", "created": prev_key, "cancelled": key, "index": i})

        # Oscillations: A→B→A (check i-2)
        if i >= 2:
            prev2 = history[i - 2]
            prev2_key = f"{prev2['method']} {_normalize(prev2['path'])}"
            if key == prev2_key and key != prev_key:
                oscillations += 1

    retry_rate = retries / max(total - 1, 1)
    error_rate = errors / max(total, 1)
    correction_rate = corrections / max(total - 1, 1)
    abandon_rate = abandons / max(total - 1, 1)
    oscillation_rate = oscillations / max(total - 2, 1)

    # Composite confusion score: weighted sum (0 = no confusion, 1 = total mess)
    confusion_score = (
        0.3 * error_rate +
        0.25 * retry_rate +
        0.2 * correction_rate +
        0.15 * abandon_rate +
        0.1 * oscillation_rate
    )

    return {
        "confusion_score": round(min(confusion_score, 1.0), 4),
        "retry_rate": round(retry_rate, 4),
        "error_rate": round(error_rate, 4),
        "correction_rate": round(correction_rate, 4),
        "abandon_rate": round(abandon_rate, 4),
        "oscillation_rate": round(oscillation_rate, 4),
        "confusion_examples": examples,
    }


def compute_score(metrics: dict, baseline: dict | None) -> dict:
    """Compute the composite api_score relative to baseline.

    Fixed budget: agent gets N experiments. Score = how good did it get?

    quality = log10(1 + final_score)   [0→0, 1→0.3, 3→0.6, 9→1.0]
    api_score = norm_quality × (1 - failure_rate) × (1 - confusion) / norm_cost

    Higher = better API. Baseline = 1.0.
    """
    import math

    raw_score = metrics.get("final_score", 0)
    # log10(1 + score): monotonic, handles any positive value
    quality = math.log10(1 + max(raw_score, 0))

    total_exps = metrics.get("experiments_completed", 0) + metrics.get("experiments_failed", 0)
    failure_rate = metrics.get("experiments_failed", 0) / max(total_exps, 1)
    confusion = metrics.get("confusion_score", 0)

    if baseline:
        baseline_quality = math.log10(1 + max(baseline.get("final_score", 0), 0))
        norm_quality = quality / max(baseline_quality, 0.001)
        norm_cost = metrics.get("cost_total", 1) / max(baseline.get("cost_total", 1), 0.001)
    else:
        norm_quality = quality
        norm_cost = 1.0

    api_score = norm_quality * (1 - failure_rate) * (1 - confusion) / max(norm_cost, 0.001)

    return {
        "api_score": round(api_score, 4),
        "quality_log": round(quality, 4),
        "norm_quality": round(norm_quality, 4),
        "failure_rate": round(failure_rate, 4),
        "norm_cost": round(norm_cost, 4),
    }


def main():
    args = parse_args()

    # Load baseline if it exists
    baseline = None
    if os.path.exists(args.baseline):
        baseline = json.loads(Path(args.baseline).read_text())

    # Set up temp dir with test project copy
    work_dir = Path(tempfile.mkdtemp(prefix="lab_eval_"))
    project_dir = work_dir / "project"
    copy_test_project(project_dir)

    # Find a free port
    import socket
    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    inner_url = f"http://{_get_host()}:{port}"
    print(f"Starting inner Lab on {inner_url} ...", file=sys.stderr)
    lab_proc = start_lab(str(project_dir), port)
    build_dashboard_async(REPO_ROOT)

    try:
        t0 = time.time()

        print(f"Launching inner agent ({args.model})...", file=sys.stderr)
        launch_agent(str(project_dir), port, args.model, args.timeout, args.budget,
                     max_cost=args.max_cost)

        wall_time = time.time() - t0

        # Collect results
        print("Collecting metrics...", file=sys.stderr)
        api_stats = collect_api_stats(port)
        lab_state = collect_lab_state(port)
        token_breakdown = parse_session_tokens(project_dir, t0)

        ideas = lab_state["ideas"]
        experiments = lab_state["experiments"]
        graph = lab_state["graph"]
        completed = [e for e in experiments if e.get("status") == "completed"]
        failed = [e for e in experiments if e.get("status") == "failed"]

        # Analyze idea DAG for branching behavior
        dag_metrics = analyze_idea_dag(graph, ideas)

        # Build per-experiment API stats and confusion metrics from call history
        history = api_stats.get("history", [])
        per_experiment = build_per_experiment_stats(experiments, history)
        confusion = compute_confusion_metrics(history)

        # Find best score across completed experiments
        best_score = 0.0
        for exp in completed:
            score = (exp.get("metrics") or {}).get("score", 0)
            if score > best_score:
                best_score = score

        ideas_created = len(ideas)
        calls_per_idea = api_stats["total_calls"] / max(ideas_created, 1)
        tokens_per_idea = token_breakdown.get("tokens_total", 0) / max(ideas_created, 1)

        metrics = {
            "final_score": round(best_score, 6),
            "total_api_calls": api_stats["total_calls"],
            "ideas_created": ideas_created,
            "experiments_completed": len(completed),
            "experiments_failed": len(failed),
            "calls_per_idea": round(calls_per_idea, 1),
            "tokens_per_idea": round(tokens_per_idea, 0),
            "wall_time_s": round(wall_time, 1),
            **dag_metrics,
            "confusion_score": confusion["confusion_score"],
            "retry_rate": confusion["retry_rate"],
            "error_rate": confusion["error_rate"],
            "correction_rate": confusion["correction_rate"],
            "abandon_rate": confusion["abandon_rate"],
            "oscillation_rate": confusion["oscillation_rate"],
            **token_breakdown,
        }

        # Compute composite score
        score_breakdown = compute_score(metrics, baseline)
        metrics.update(score_breakdown)

        meta = {
            "model": args.model,
            "budget_cap": args.budget,
            "budget": args.budget,
            "test_project": "fast_math",
            "port": port,
            "inner_lab_url": f"http://{_get_host()}:{port}",
            "baseline_used": args.baseline if baseline else None,
            "confusion_examples": confusion["confusion_examples"],
            "per_experiment_stats": per_experiment,
            "idea_dag": {
                "nodes": [{"id": n["id"], "description": n.get("description", ""),
                            "status": n.get("status", ""), "parent_ids": n.get("parent_ids", [])}
                           for n in graph.get("nodes", [])],
                "edges": graph.get("edges", []),
            },
        }

        output = {"metrics": metrics, "meta": meta}

        # Human-readable summary to stderr
        print(f"\n{'='*50}", file=sys.stderr)
        print(f"API Score:              {metrics['api_score']}", file=sys.stderr)
        print(f"Final score:            {metrics['final_score']}", file=sys.stderr)
        print(f"Quality (log):          {metrics['quality_log']}", file=sys.stderr)
        print(f"API calls:              {metrics['total_api_calls']}", file=sys.stderr)
        print(f"Calls/idea:             {metrics['calls_per_idea']}", file=sys.stderr)
        print(f"Cost total:             ${metrics['cost_total']}", file=sys.stderr)
        print(f"  bash:                 ${metrics['cost_bash']}", file=sys.stderr)
        print(f"  context:              ${metrics['cost_context']}", file=sys.stderr)
        print(f"  reasoning:            ${metrics['cost_reasoning']}", file=sys.stderr)
        print(f"Wall time:              {metrics['wall_time_s']}s", file=sys.stderr)
        print(f"Confusion score:        {metrics['confusion_score']}", file=sys.stderr)
        print(f"  retries:              {metrics['retry_rate']:.1%}", file=sys.stderr)
        print(f"  errors:               {metrics['error_rate']:.1%}", file=sys.stderr)
        print(f"  corrections:          {metrics['correction_rate']:.1%}", file=sys.stderr)
        print(f"  oscillations:         {metrics['oscillation_rate']:.1%}", file=sys.stderr)
        print(f"DAG depth:              {metrics['dag_max_depth']}", file=sys.stderr)
        print(f"Ideas with parents:     {metrics['dag_ideas_with_parents']}", file=sys.stderr)
        print(f"Ideas abandoned:        {metrics['dag_ideas_abandoned']}", file=sys.stderr)
        print(f"Branching ratio:        {metrics['dag_branching_ratio']}", file=sys.stderr)
        if per_experiment:
            print(f"Per-experiment calls:   {[p['api_calls'] for p in per_experiment]}", file=sys.stderr)
        print(f"{'='*50}", file=sys.stderr)

        # Lab-compatible JSON output
        json_str = json.dumps(output)
        # Always print to stdout (Lab captures last JSON line from experiment log)
        print(json_str)
        if args.output:
            Path(args.output).write_text(json_str + "\n")
            print(f"\nMetrics written to {args.output}", file=sys.stderr)

    finally:
        lab_proc.send_signal(signal.SIGTERM)
        try:
            lab_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            lab_proc.kill()
        # Clean up
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
