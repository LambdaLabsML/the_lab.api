#!/usr/bin/env python3 -u
"""Evaluate an ARC solver by launching an inner Claude Code agent.

Starts a Lab instance, copies the arc3_autosolver project to a temp dir,
launches an inner agent (Claude Code + Gemma via LiteLLM), and collects
the best score from completed experiments.

Usage (from experiment scripts in the outer Lab):
    python .the_lab/artifacts/run_eval_arc.py --model gemma-4-31b --budget 10 --timeout 7200

Metrics output (Lab-compatible JSON on stdout):
    - score: best ARC scorecard score achieved
    - wall_seconds, total_experiments, best_experiment, etc.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

# Force unbuffered output
os.environ["PYTHONUNBUFFERED"] = "1"
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# Child process tracking
# ---------------------------------------------------------------------------
_child_procs: list[subprocess.Popen] = []
_child_lock = threading.Lock()


def _track_child(proc: subprocess.Popen):
    with _child_lock:
        _child_procs.append(proc)


def _kill_all_children():
    with _child_lock:
        procs = list(_child_procs)
    for proc in procs:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
    for proc in procs:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass


atexit.register(_kill_all_children)


def _signal_handler(signum, frame):
    _kill_all_children()
    sys.exit(128 + signum)


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
# The outer Lab's project dir (where the experiment runs)
# REPO_ROOT = the worktree where the experiment runs (optimization/proj/ on an idea branch).
# Contains the_lab/ (outer agent's edits to API code, PROMPT_api.md, agent_skills).
REPO_ROOT = Path(os.environ.get("THE_LAB_REPO", os.getcwd())).resolve()
# Fixed ARC project files — accessed via symlink in .the_lab/artifacts/
ARC_PROJECT_SRC = REPO_ROOT / ".the_lab" / "artifacts" / "arc3_autosolver"
if not ARC_PROJECT_SRC.exists():
    # Fallback: direct path from script location
    ARC_PROJECT_SRC = SCRIPT_DIR / "arc3_autosolver"


def _get_host() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostname()


def _lab_get(url: str, timeout: float = 5):
    req = urllib.request.Request(url, headers={"X-The-Lab-Source": "dashboard"})
    return urllib.request.urlopen(req, timeout=timeout)


def copy_project(dest: Path):
    """Set up a temp dir with ARC project files + branch's PROMPT_api.md.

    Structure mirrors the self-optimization:
    - ARC project files (agent/, evaluate_agent.py) from the fixed source
    - PROMPT_generated.md built from ARC PROMPT.md + branch's PROMPT_api.md
    - Agent skills from the branch's the_lab/agent_skills/
    """
    # 1. Copy fixed ARC project files
    shutil.copytree(
        ARC_PROJECT_SRC, dest, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".the_lab", ".git", "__pycache__", ".venv", ".claude", ".mcp.json"),
    )

    # 2. Build PROMPT_generated.md = ARC PROMPT.md + branch's PROMPT_api.md
    prompt_src = dest / "PROMPT.md"
    # Use the branch's PROMPT_api.md (outer agent may have edited it)
    prompt_api = REPO_ROOT / "the_lab" / "PROMPT_api.md"
    if not prompt_api.exists():
        prompt_api = SCRIPT_DIR.parent / "the_lab" / "PROMPT_api.md"

    parts = []
    if prompt_src.exists():
        parts.append(prompt_src.read_text().strip())
    if prompt_api.exists():
        parts.append(prompt_api.read_text().strip())
    (dest / "PROMPT_generated.md").write_text("\n\n".join(parts) + "\n")

    # 3. Copy agent skills from the branch (outer agent may have edited these)
    agent_skills = REPO_ROOT / "the_lab" / "agent_skills"
    if not agent_skills.exists():
        agent_skills = SCRIPT_DIR.parent / "the_lab" / "agent_skills"
    if agent_skills.exists():
        claude_dir = dest / ".claude"
        claude_dir.mkdir(exist_ok=True)
        skills_src = agent_skills / "skills"
        if skills_src.exists():
            shutil.copytree(skills_src, claude_dir / "skills", dirs_exist_ok=True)
        mcp_src = agent_skills / "mcp.json"
        if mcp_src.exists():
            shutil.copy2(mcp_src, dest / ".mcp.json")
        settings_src = agent_skills / "settings.json"
        if settings_src.exists():
            shutil.copy2(settings_src, claude_dir / "settings.json")

    # 4. Init git
    if not (dest / ".git").exists():
        subprocess.run(["git", "init"], cwd=str(dest), capture_output=True)
        subprocess.run(["git", "config", "user.name", "eval"], cwd=str(dest), capture_output=True)
        subprocess.run(["git", "config", "user.email", "eval@local"], cwd=str(dest), capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(dest), capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=str(dest), capture_output=True)

    print(f"  Copied ARC project to {dest}", file=sys.stderr)
    print(f"  PROMPT_api.md from: {prompt_api}", file=sys.stderr)


def start_lab(repo_dir: str, port: int) -> subprocess.Popen:
    """Start a Lab instance for the ARC project."""
    sandbox_conf = Path(repo_dir) / ".the_lab" / "sandbox" / "config.json"
    sandbox_conf.parent.mkdir(parents=True, exist_ok=True)
    sandbox_conf.write_text(json.dumps({"enabled": False}) + "\n")

    env = {**os.environ, "THE_LAB_REPO": repo_dir}
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
        start_new_session=True,
    )
    _track_child(proc)

    host = _get_host()
    print(f"  Waiting for Lab on {host}:{port}...", file=sys.stderr)
    for i in range(60):
        rc = proc.poll()
        if rc is not None:
            raise RuntimeError(f"Lab crashed on startup (exit code {rc})")
        try:
            _lab_get(f"http://{host}:{port}/api/v1/backlog", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError(f"Lab failed to start on port {port}")

    print(f"  Lab running on {host}:{port}", file=sys.stderr)
    return proc


def launch_inner_agent(
    work_dir: str, api_port: int, model: str, timeout: int, budget: int,
) -> dict:
    """Launch inner Claude Code agent with Gemma model, wait for completion."""
    host = _get_host()
    api_base = f"http://{host}:{api_port}/api/v1"

    instruction = (
        f"You are working on an ARC-AGI-3 puzzle solver. Your job is to edit agent/arc_agent.py to maximize the score.\n\n"
        f"START NOW — do these steps in order:\n\n"
        f"Step 1: Read PROMPT_generated.md\n"
        f"Step 2: Read agent/arc_agent.py\n"
        f"Step 3: Read evaluate_agent.py\n"
        f"Step 4: Create an idea using the Lab API:\n"
        f"  curl -s -X POST {api_base}/ideas/new -H 'Content-Type: application/json' -d '{{\"description\": \"Heuristic solver: analyze grid patterns\"}}'\n"
        f"Step 5: Edit agent/arc_agent.py with an improved solver\n"
        f"Step 6: Create and run an experiment:\n"
        f"  curl -s -X POST {api_base}/ideas/1/experiments -H 'Content-Type: application/json' "
        f"-d '{{\"description\": \"test heuristic\", \"script_content\": \"#!/bin/bash\\nset -euo pipefail\\ncd {work_dir}\\npython evaluate_agent.py\"}}'\n"
        f"Step 7: Wait for the experiment to finish:\n"
        f"  curl -s '{api_base}/wait?experiment_id=1.1&timeout=600'\n"
        f"Step 8: Read the results and iterate — create new ideas and experiments to improve the score.\n\n"
        f"IMPORTANT: Do NOT ask questions. Do NOT wait for confirmation. Execute each step immediately."
    )

    env = {**os.environ}
    env["THE_LAB_API_URL"] = api_base
    env["VLLM_BASE"] = os.environ.get("VLLM_BASE", "http://localhost:8008/v1")
    env["VLLM_MODEL"] = model

    # Use the branch's gemma_agent.py (editable by the outer optimization agent).
    # Falls back to optimization/gemma_agent.py (source of truth), then the
    # external model_inference/simple_agent.py as a last resort.
    simple_agent = REPO_ROOT / "gemma_agent.py"
    if not simple_agent.exists():
        simple_agent = SCRIPT_DIR / "gemma_agent.py"
    if not simple_agent.exists():
        simple_agent = Path("/lambda/nfs/architects-us-south-2/model_inference/simple_agent.py")

    cmd = [
        sys.executable, str(simple_agent),
        "--cwd", work_dir,
        "--max-turns", str(budget * 20),  # ~20 turns per experiment
        "--system-prompt",
        "You are an autonomous coding agent working on an ARC-AGI-3 puzzle solver. "
        "Complete the entire task without asking questions. Never ask for confirmation. "
        "Use bash to call curl for the Lab API. Use read_file, write_file, bash tools. "
        "Keep working until you have created ideas, edited code, run experiments, and checked results.",
        "-p", instruction,
    ]

    print(f"  Launching inner agent (model={model})...", file=sys.stderr)
    print(f"  cmd: {' '.join(cmd[:8])}...", file=sys.stderr)
    print(f"  cwd: {work_dir}", file=sys.stderr)
    print(f"  ANTHROPIC_BASE_URL: {env.get('ANTHROPIC_BASE_URL', 'not set')}", file=sys.stderr)
    # Verify key files exist in work dir
    for f in ["PROMPT_generated.md", "PROMPT.md", ".claude/skills/lab_api_mcp.py", "agent/arc_agent.py", "evaluate_agent.py"]:
        exists = (Path(work_dir) / f).exists()
        print(f"  {f}: {'OK' if exists else 'MISSING'}", file=sys.stderr)
    proc = subprocess.Popen(
        cmd, cwd=work_dir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _track_child(proc)

    # Monitor: wait for timeout or budget (poll experiments via API)
    t_start = time.time()
    last_exp_count = 0

    while proc.poll() is None:
        elapsed = time.time() - t_start
        if elapsed > timeout:
            print(f"  Timeout ({timeout}s) reached, stopping agent", file=sys.stderr)
            break

        # Check experiment count
        try:
            resp = _lab_get(f"{api_base}/chart-data", timeout=3)
            data = json.loads(resp.read())
            exp_count = len(data.get("experiments", []))
            if exp_count > last_exp_count:
                print(f"  [{int(elapsed)}s] {exp_count} experiments completed", file=sys.stderr)
                last_exp_count = exp_count
            if exp_count >= budget:
                print(f"  Budget ({budget} experiments) reached, stopping agent", file=sys.stderr)
                break
        except Exception:
            pass

        time.sleep(10)

    # Kill agent
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass

    wall_time = time.time() - t_start

    # Dump agent output for debugging
    rc = proc.poll()
    print(f"  Agent exited with code {rc} after {wall_time:.0f}s", file=sys.stderr)
    if proc.stdout:
        output_tail = proc.stdout.read().decode(errors="replace")[-3000:]
        if output_tail.strip():
            print(f"  Agent output (last 3000 chars):\n{output_tail}", file=sys.stderr)

    # Collect results from the inner Lab — pull all experiment metrics
    results: dict = {"wall_seconds": round(wall_time, 2)}
    try:
        resp = _lab_get(f"{api_base}/chart-data", timeout=5)
        data = json.loads(resp.read())
        experiments = data.get("experiments", [])
        results["total_experiments"] = len(experiments)
        results["total_ideas"] = len(set(e.get("idea_id") for e in experiments))

        # Find best experiment by 'score' metric
        best_score = 0
        best_metrics: dict = {}
        best_label = None
        for exp in experiments:
            m = exp.get("metrics", {})
            s = m.get("score", 0)
            if isinstance(s, (int, float)) and s > best_score:
                best_score = s
                best_metrics = m
                best_label = exp.get("label", exp.get("id"))

        results["best_score"] = best_score
        results["best_experiment"] = best_label
        # Include all metrics from the best experiment (score, wall_seconds,
        # levels_completed, envs_completed, etc.)
        for k, v in best_metrics.items():
            if isinstance(v, (int, float)):
                results[f"best_{k}"] = v

        # Summary stats across all experiments
        all_scores = [e.get("metrics", {}).get("score", 0) for e in experiments
                      if isinstance(e.get("metrics", {}).get("score", 0), (int, float))]
        if all_scores:
            results["mean_score"] = round(sum(all_scores) / len(all_scores), 4)
            results["max_score"] = max(all_scores)

        # Count failed/successful
        results["experiments_completed"] = sum(
            1 for e in experiments if e.get("status") == "completed")
        results["experiments_failed"] = sum(
            1 for e in experiments if e.get("status") == "failed")

    except Exception as e:
        print(f"  Warning: could not read final results: {e}", file=sys.stderr)
        results.setdefault("total_experiments", 0)
        results.setdefault("best_score", 0)

    return results


def main():
    parser = argparse.ArgumentParser(description="ARC-AGI-3 evaluation via inner agent")
    parser.add_argument("--model", default="gemma-4-31b", help="Model for the inner agent")
    parser.add_argument("--budget", type=int, default=10, help="Max experiments before stopping")
    parser.add_argument("--timeout", type=int, default=7200, help="Max seconds (default: 2h)")
    parser.add_argument("--port", type=int, default=0, help="Lab port (0 = random)")
    args = parser.parse_args()

    if args.port == 0:
        import random
        args.port = random.randint(10000, 60000)

    print(f"ARC eval: model={args.model}, budget={args.budget}, timeout={args.timeout}s", file=sys.stderr)

    # 1. Copy project to temp dir
    work_dir = Path(tempfile.mkdtemp(prefix="lab_arc_eval_"))
    try:
        copy_project(work_dir)

        # 2. Start inner Lab
        lab_proc = start_lab(str(work_dir), args.port)

        # 3. Launch inner agent
        results = launch_inner_agent(
            str(work_dir), args.port, args.model, args.timeout, args.budget,
        )

        # 4. Output metrics
        print(f"\n=== ARC Eval Results ===", file=sys.stderr)
        for k, v in results.items():
            print(f"  {k}: {v}", file=sys.stderr)

        print(json.dumps({"metrics": results}))

    finally:
        _kill_all_children()
        # Clean up temp dir
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
