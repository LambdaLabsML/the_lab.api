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
TESTS_DIR = SCRIPT_DIR / "tests"

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
    p.add_argument("--agent", default="claude", choices=["claude", "codex"],
                   help="Which agent CLI to use (default: claude)")
    p.add_argument("--model", default="haiku", help="Model for inner agent (haiku|sonnet|opus for claude; o4-mini|o3 for codex)")
    p.add_argument("--budget", type=int, default=15, help="Max experiments before giving up")
    p.add_argument("--timeout", type=int, default=900, help="Wall-time cap in seconds")
    p.add_argument("--max-cost", type=float, default=0, help="Max dollar cost for inner agent (0 = no limit)")
    p.add_argument("--baseline", default=str(SCRIPT_DIR / "baseline.json"),
                   help="Path to baseline.json for score normalization")
    p.add_argument("--output", default=None,
                   help="Write metrics JSON to this file (default: stdout)")
    p.add_argument("--tests", default="t1,t2,t3,t4,t5,t6,t7",
                   help="Comma-separated test IDs to run (default: t1,t2,t3,t4,t5,t6,t7)")
    p.add_argument("--single-test", default=None,
                   help="Run a single test fixture dir (for internal use by the multi-test runner)")
    return p.parse_args()


def copy_test_project(dest: Path):
    """Copy the old test_project fixture to dest (legacy single-test mode)."""
    _copy_fixture(TEST_PROJECT_SRC, dest)


def copy_test_fixture(test_id: str, dest: Path):
    """Copy a test fixture (T1-T4) to dest with PROMPT_api.md appended."""
    test_names = {
        "t1": "t1_branching",
        "t2": "t2_experiment_mgmt",
        "t3": "t3_error_recovery",
        "t4": "t4_leaderboard_search",
        "t5": "t5_discovery",
        "t6": "t6_multi_branch",
        "t7": "t7_analytics",
    }
    test_name = test_names.get(test_id, test_id)
    fixture_src = TESTS_DIR / test_name / "fixture"
    prompt_src = TESTS_DIR / test_name / "PROMPT_problem.md"

    if not fixture_src.exists():
        raise RuntimeError(f"Fixture not found: {fixture_src}. Run: python optimization/tests/seed_fixture.py {test_id}")

    # Copy the pre-seeded fixture (includes .git, .the_lab, benchmark/)
    shutil.copytree(fixture_src, dest, dirs_exist_ok=True)

    # Build PROMPT.md = PROMPT_problem.md + PROMPT_api.md
    prompt_dst = dest / "PROMPT.md"
    parts = []
    if prompt_src.exists():
        parts.append(prompt_src.read_text().strip())
    elif prompt_dst.exists():
        parts.append(prompt_dst.read_text().strip())

    # T5 uses a stripped PROMPT_api.md (key endpoints omitted for discovery test)
    if test_id == "t5":
        stripped_api = TESTS_DIR / "t5_discovery" / "PROMPT_api_stripped.md"
        if stripped_api.exists():
            parts.append(stripped_api.read_text().strip())
        else:
            print(f"  WARNING: PROMPT_api_stripped.md not found for T5", file=sys.stderr)
    else:
        # Append API instructions from the branch being tested
        prompt_api = REPO_ROOT / "PROMPT_api.md"
        if prompt_api.exists():
            parts.append(prompt_api.read_text().strip())
        else:
            # Fallback: use the one shipped with the_lab package
            pkg_api = Path(__file__).resolve().parent.parent / "the_lab" / "PROMPT_api.md"
            if pkg_api.exists():
                parts.append(pkg_api.read_text().strip())
    prompt_dst.write_text("\n\n".join(parts) + "\n")
    print(f"  Built PROMPT.md for {test_id}", file=sys.stderr)


def _copy_fixture(src: Path, dest: Path):
    """Copy a fixture dir and append PROMPT_api.md."""
    shutil.copytree(src, dest, dirs_exist_ok=True)
    prompt_api = REPO_ROOT / "PROMPT_api.md"
    if prompt_api.exists():
        prompt_dst = dest / "PROMPT.md"
        with open(prompt_dst, "a") as f:
            f.write("\n")
            f.write(prompt_api.read_text())
        print(f"  Appended PROMPT_api.md", file=sys.stderr)
    # Init git if not already a repo
    if not (dest / ".git").exists():
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
    tag: str = "",
    **kwargs,
) -> None:
    """Launch an inner agent (claude or codex), stop when budget reached or timeout."""
    pfx = f"[{tag}] " if tag else "  "
    agent_type = kwargs.get("agent", "claude")

    instruction = (
        f"You have access to a local experiment management API at http://localhost:{api_port}/api/v1. "
        f"Read PROMPT.md for your task and the API workflow. "
        f"Follow the workflow: create idea, checkout, edit benchmark/kernels.py, "
        f"create experiment with script_content='#!/bin/bash\\nset -euo pipefail\\npython benchmark/eval_harness.py', "
        f"start it, wait, check results. Iterate to maximize the composite score. "
        f"Branch from successful ideas for further improvements."
    )

    env = {**os.environ}
    max_cost = kwargs.get("max_cost", 0)

    # Build agent-specific command
    if agent_type == "codex":
        cmd = [
            "codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m", model,
            "--json",
        ]
        cmd.append(instruction)
    else:
        cmd = [
            "claude", "--dangerously-skip-permissions",
            "--model", model,
            "--print", "--verbose", "--output-format", "stream-json",
        ]
        if max_cost > 0:
            cmd.extend(["--max-budget-usd", str(max_cost)])
        cmd.extend(["-p", instruction])

    proc = subprocess.Popen(
        cmd, cwd=prompt_path, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    # Buffer detailed log for post-hoc replay
    log_buffer = kwargs.get("log_buffer")

    def _stream_progress_claude():
        """Parse Claude stream-json events."""
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
                        if log_buffer is not None:
                            log_buffer.append(f"  [{name}] {preview}")
                    elif c.get("type") == "text":
                        text = c.get("text", "")
                        if text.strip():
                            short = text.strip()[:100]
                            if log_buffer is not None:
                                log_buffer.append(f"  > {short}")
            elif t == "user":
                msg = evt.get("message", {})
                for c in msg.get("content", []):
                    content = c.get("content", "")
                    if isinstance(content, str) and content.strip():
                        first_line = content.strip().split("\n")[0][:120]
                        if log_buffer is not None:
                            log_buffer.append(f"    ← {first_line}")
            elif t == "result":
                cost = evt.get("total_cost_usd", 0)
                turns = evt.get("num_turns", 0)
                if log_buffer is not None:
                    log_buffer.append(f"  [done] {turns} turns, ${cost:.3f}")

    def _stream_progress_codex():
        """Parse Codex JSON events."""
        for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt_type = evt.get("type", "")
            payload = evt.get("payload") or evt
            if evt_type == "function_call" or evt_type == "tool_use":
                name = payload.get("name", "?")
                args = payload.get("arguments", "")
                if isinstance(args, str):
                    preview = args[:80]
                else:
                    preview = str(args)[:80]
                if log_buffer is not None:
                    log_buffer.append(f"  [{name}] {preview}")
            elif evt_type == "message" or evt_type == "response":
                text = ""
                content = payload.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c.get("text", "")
                            break
                if text.strip():
                    if log_buffer is not None:
                        log_buffer.append(f"  > {text.strip()[:100]}")
            elif evt_type == "function_call_output":
                output = str(payload.get("output", ""))[:120]
                if output and log_buffer is not None:
                    log_buffer.append(f"    ← {output.split(chr(10))[0]}")

    import threading
    stream_fn = _stream_progress_codex if agent_type == "codex" else _stream_progress_claude
    stream_thread = threading.Thread(target=stream_fn, daemon=True)
    stream_thread.start()

    # Poll the inner Lab for experiment count — kill agent when budget reached
    # Write progress to $THE_LAB_PROGRESS if set (for the outer Lab's UI)
    import urllib.request
    progress_file = os.environ.get("THE_LAB_PROGRESS")
    start = time.time()
    deadline = start + timeout

    # Snapshot initial experiment count (pre-seeded fixtures have existing experiments)
    initial_completed = 0
    try:
        resp = _lab_get(f"http://{_get_host()}:{api_port}/api/v1/chart-data", timeout=5)
        data = json.loads(resp.read())
        initial_completed = len(data.get("experiments", []))
        print(f"{pfx}Pre-seeded: {initial_completed} experiments (budget counts new only)", file=sys.stderr)
    except Exception:
        pass

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
            total_completed = len(data.get("experiments", []))
            n_completed = total_completed - initial_completed  # only new experiments
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

        # Log progress to terminal — only when something changes
        progress_key = (n_completed, n_running)
        if not hasattr(launch_agent, '_last_progress'):
            launch_agent._last_progress = {}
        last = launch_agent._last_progress.get(tag)
        if progress_key != last and (n_completed > 0 or n_running > 0):
            launch_agent._last_progress[tag] = progress_key
            print(f"{pfx}[{int(elapsed)}s] exp: {n_completed}/{budget} done, "
                  f"{n_running} running | best: {best_score:.4f}",
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
            print(f"{pfx}Budget reached ({n_completed}/{budget}). Stopping.",
                  file=sys.stderr)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            break

    if proc.poll() is None:
        print(f"{pfx}Timeout reached. Killing agent.", file=sys.stderr)
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


def find_session_jsonl(project_dir: Path, start_time: float, agent: str = "claude") -> list[Path]:
    """Find session JSONL files for the inner agent.

    Claude: ~/.claude/projects/<mangled-path>/*.jsonl
    Codex:  ~/.codex/sessions/YYYY/MM/DD/*.jsonl
    """
    files = []
    if agent == "codex":
        sessions_dir = Path.home() / ".codex" / "sessions"
        if sessions_dir.exists():
            for f in sorted(sessions_dir.rglob("*.jsonl")):
                if f.stat().st_mtime >= start_time:
                    files.append(f)
    else:
        import re
        resolved = str(project_dir.resolve()).lstrip("/")
        mangled = "-" + re.sub(r"[^a-zA-Z0-9]", "-", resolved)
        session_dir = Path.home() / ".claude" / "projects" / mangled
        if session_dir.exists():
            for f in sorted(session_dir.glob("*.jsonl")):
                if f.stat().st_mtime >= start_time:
                    files.append(f)
    if files:
        print(f"  Found {len(files)} {agent} session file(s)", file=sys.stderr)
    else:
        print(f"  Warning: no {agent} session JSONL found", file=sys.stderr)
    return files


def _parse_claude_line(obj: dict, categories: dict):
    """Parse a single Claude session JSONL line into token categories."""
    msg = obj.get("message") if isinstance(obj, dict) else None
    if not isinstance(msg, dict):
        return
    usage = msg.get("usage")
    if not usage:
        return
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
            cat = "bash"
    else:
        stop = msg.get("stop_reason", "none")
        cat = "reasoning" if stop == "end_turn" else "context"
    categories[cat]["calls"] += 1
    categories[cat]["input"] += usage.get("input_tokens", 0)
    categories[cat]["output"] += usage.get("output_tokens", 0)
    categories[cat]["cache_create"] += usage.get("cache_creation_input_tokens", 0)
    categories[cat]["cache_read"] += usage.get("cache_read_input_tokens", 0)


def _parse_codex_line(obj: dict, categories: dict):
    """Parse a single Codex session JSONL line into token categories."""
    if not isinstance(obj, dict):
        return
    evt_type = obj.get("type", "")
    payload = obj.get("payload") or obj

    # Codex usage is in response events
    usage = payload.get("usage") or {}
    if not usage and isinstance(payload, dict):
        # Try nested in response
        resp = payload.get("response") or {}
        usage = resp.get("usage") or {}

    input_tokens = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
    output_tokens = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)

    if not input_tokens and not output_tokens:
        return

    # Categorize by event type
    if evt_type in ("function_call", "tool_use"):
        name = payload.get("name", "")
        if name in ("read", "grep", "glob", "Read", "Grep", "Glob"):
            cat = "read_search"
        elif name in ("edit", "write", "Edit", "Write", "apply_diff", "patch"):
            cat = "edit_write"
        elif name in ("bash", "Bash", "shell", "execute"):
            cat = "bash"
        else:
            cat = "bash"
    elif evt_type in ("response", "message"):
        cat = "reasoning"
    else:
        cat = "context"

    categories[cat]["calls"] += 1
    categories[cat]["input"] += input_tokens
    categories[cat]["output"] += output_tokens


def parse_session_tokens(project_dir: Path, start_time: float = 0, agent: str = "claude") -> dict:
    """Parse inner agent's session JSONL for token breakdown."""
    categories = {
        "bash": {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "calls": 0},
        "context": {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "calls": 0},
        "reasoning": {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "calls": 0},
        "read_search": {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "calls": 0},
        "edit_write": {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "calls": 0},
    }

    session_files = find_session_jsonl(project_dir, start_time, agent=agent)

    for f in session_files:
        for line in open(f):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if agent == "codex":
                _parse_codex_line(obj, categories)
            else:
                _parse_claude_line(obj, categories)

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


def run_single_test(test_id: str, args) -> dict:
    """Run a single test (T1-T4) and return its results dict."""
    pfx = f"[{test_id}] "
    print(f"\n{pfx}{'='*40}", file=sys.stderr)
    print(f"{pfx}Starting test", file=sys.stderr)
    print(f"{pfx}{'='*40}", file=sys.stderr)

    work_dir = Path(tempfile.mkdtemp(prefix=f"lab_eval_{test_id}_"))
    project_dir = work_dir / "project"
    copy_test_fixture(test_id, project_dir)

    import socket
    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    host = _get_host()
    inner_url = f"http://{host}:{port}"
    print(f"{pfx}Lab on {inner_url}", file=sys.stderr)
    lab_proc = start_lab(str(project_dir), port)
    build_dashboard_async(REPO_ROOT)

    try:
        t0 = time.time()
        agent_type = getattr(args, "agent", "claude")
        print(f"{pfx}Launching {agent_type} agent ({args.model})...", file=sys.stderr)
        log_buf: list[str] = []
        launch_agent(str(project_dir), port, args.model, args.timeout, args.budget,
                     tag=test_id, max_cost=args.max_cost, log_buffer=log_buf,
                     agent=agent_type)
        wall_time = time.time() - t0

        # Collect standard metrics
        print(f"{pfx}Collecting metrics...", file=sys.stderr)
        api_stats = collect_api_stats(port)
        lab_state = collect_lab_state(port)
        token_breakdown = parse_session_tokens(project_dir, t0, agent=agent_type)
        ideas = lab_state["ideas"]
        experiments = lab_state["experiments"]
        history = api_stats.get("history", [])
        confusion = compute_confusion_metrics(history)

        # Run the test-specific scoring script
        test_score = {}
        score_module = TESTS_DIR / f"{_test_dir_name(test_id)}" / "score.py"
        if score_module.exists():
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(f"score_{test_id}", str(score_module))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                test_score = mod.score(f"http://{host}:{port}/api/v1")
                print(f"{pfx}Score: {test_score.get('score', '?')}", file=sys.stderr)
            except Exception as e:
                print(f"{pfx}Scoring failed: {e}", file=sys.stderr)
                test_score = {"test": test_id, "score": 0.0, "error": str(e)}

        return {
            "test_id": test_id,
            "test_score": test_score,
            "wall_time_s": round(wall_time, 1),
            "total_api_calls": api_stats.get("total_calls", 0),
            "confusion_score": confusion["confusion_score"],
            "inner_lab_url": inner_url,
            "log": log_buf,
            **token_breakdown,
        }
    finally:
        lab_proc.send_signal(signal.SIGTERM)
        try:
            lab_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            lab_proc.kill()
        shutil.rmtree(work_dir, ignore_errors=True)


def _test_dir_name(test_id: str) -> str:
    names = {"t1": "t1_branching", "t2": "t2_experiment_mgmt",
             "t3": "t3_error_recovery", "t4": "t4_leaderboard_search",
             "t5": "t5_discovery", "t6": "t6_multi_branch",
             "t7": "t7_analytics"}
    return names.get(test_id, test_id)


def main():
    args = parse_args()
    test_ids = [t.strip() for t in args.tests.split(",") if t.strip()]

    # If running multi-test mode (default)
    if len(test_ids) > 1 or (len(test_ids) == 1 and test_ids[0] in ("t1", "t2", "t3", "t4", "t5", "t6", "t7")):
        import math
        from concurrent.futures import ThreadPoolExecutor, as_completed

        print(f"Running {len(test_ids)} tests: {', '.join(test_ids)}", file=sys.stderr)
        results = {}

        # Run tests concurrently (each gets its own Lab instance + agent)
        with ThreadPoolExecutor(max_workers=min(len(test_ids), 4)) as pool:
            futures = {pool.submit(run_single_test, tid, args): tid for tid in test_ids}
            for future in as_completed(futures):
                tid = futures[future]
                try:
                    results[tid] = future.result()
                except Exception as e:
                    print(f"  {tid} FAILED: {e}", file=sys.stderr)
                    results[tid] = {"test_id": tid, "test_score": {"score": 0.0, "error": str(e)}}

        # Aggregate scores
        scores = [results[t]["test_score"].get("score", 0) for t in test_ids if t in results]
        # Geometric mean of test scores
        if scores and all(s > 0 for s in scores):
            aggregate = math.exp(sum(math.log(s) for s in scores) / len(scores))
        else:
            aggregate = 0.0

        total_cost = sum(results[t].get("cost_total", 0) for t in test_ids if t in results)
        total_calls = sum(results[t].get("total_api_calls", 0) for t in test_ids if t in results)
        total_time = sum(results[t].get("wall_time_s", 0) for t in test_ids if t in results)

        metrics = {
            "api_effectiveness": round(aggregate, 4),
            **{f"{t}_score": results[t]["test_score"].get("score", 0) for t in test_ids if t in results},
            "total_api_calls": total_calls,
            "total_cost": round(total_cost, 4),
            "total_wall_time_s": round(total_time, 1),
        }

        meta = {
            "model": args.model,
            "budget": args.budget,
            "tests": test_ids,
            "per_test": results,
        }

        output = {"metrics": metrics, "meta": meta}

        # Replay each test's detailed log
        for t in test_ids:
            r = results.get(t, {})
            ts = r.get("test_score", {})
            score = ts.get("score", 0)
            print(f"\n{'='*60}", file=sys.stderr)
            print(f"  {t.upper()}: {ts.get('test', t)}  —  score: {score:.4f}", file=sys.stderr)
            print(f"{'='*60}", file=sys.stderr)
            for line in r.get("log", []):
                print(f"  {line}", file=sys.stderr)
            if ts.get("checks"):
                print(f"  --- checks ---", file=sys.stderr)
                for check, val in ts["checks"].items():
                    bar = "#" * int(val * 10)
                    print(f"    {check:<25s} {val:.2f}  {bar}", file=sys.stderr)
            print(f"  calls={r.get('total_api_calls', 0)}, "
                  f"confusion={r.get('confusion_score', 0):.3f}, "
                  f"cost=${r.get('cost_total', 0):.3f}, "
                  f"time={r.get('wall_time_s', 0):.0f}s", file=sys.stderr)

        # Final aggregate
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"  API EFFECTIVENESS: {aggregate:.4f}", file=sys.stderr)
        score_parts = []
        for t in test_ids:
            if t in results:
                s = results[t]["test_score"].get("score", 0)
                score_parts.append(f"{t}={s:.3f}")
        print(f"  {' | '.join(score_parts)}", file=sys.stderr)
        print(f"  Total cost: ${total_cost:.4f}  |  Total time: {total_time:.0f}s", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        json_str = json.dumps(output)
        print(json_str)
        if args.output:
            Path(args.output).write_text(json_str + "\n")
            print(f"\nMetrics written to {args.output}", file=sys.stderr)
        return

    # Legacy single-test mode (old test_project)
    baseline = None
    if os.path.exists(args.baseline):
        baseline = json.loads(Path(args.baseline).read_text())

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

        agent_type = getattr(args, "agent", "claude")
        print(f"Launching inner {agent_type} agent ({args.model})...", file=sys.stderr)
        launch_agent(str(project_dir), port, args.model, args.timeout, args.budget,
                     max_cost=args.max_cost, agent=agent_type)

        wall_time = time.time() - t0

        # Collect results
        print("Collecting metrics...", file=sys.stderr)
        api_stats = collect_api_stats(port)
        lab_state = collect_lab_state(port)
        token_breakdown = parse_session_tokens(project_dir, t0, agent=agent_type)

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
