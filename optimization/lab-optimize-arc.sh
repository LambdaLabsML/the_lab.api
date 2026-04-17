#!/usr/bin/env bash
set -euo pipefail

# ARC-AGI-3 Optimization via nested Lab instances
#
# Usage:
#   ./optimization/lab-optimize-arc.sh start [port]
#   ./optimization/lab-optimize-arc.sh baseline [inner-budget] [timeout] [port]
#   ./optimization/lab-optimize-arc.sh agent [outer-model]
#   ./optimization/lab-optimize-arc.sh reset
#   ./optimization/lab-optimize-arc.sh cherry-pick <commit>

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJ="$SCRIPT_DIR/arc3_autosolver"

# Activate the repo's venv if not already active
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    source "$REPO_ROOT/.venv/bin/activate"
fi

# ── Setup ───────────────────────────────────────────────────────────────────

ensure_proj() {
    # arc3_autosolver is a symlink to the real project — check it exists
    if [ ! -d "$PROJ" ]; then
        echo "Error: $PROJ does not exist."
        echo "Create or symlink it first."
        exit 1
    fi

    # Ensure .the_lab structure exists
    mkdir -p "$PROJ/.the_lab/artifacts"
    mkdir -p "$PROJ/.the_lab/sandbox"
    echo '{"enabled": false}' > "$PROJ/.the_lab/sandbox/config.json"

    # Symlink eval script into artifacts
    ln -sf "$SCRIPT_DIR/run_eval_arc.py" "$PROJ/.the_lab/artifacts/run_eval_arc.py"

    # Ensure git repo exists
    if [ ! -d "$PROJ/.git" ]; then
        cd "$PROJ"
        git init
        git add -A
        git commit -m "Initial ARC project" 2>/dev/null || true
        cd "$SCRIPT_DIR"
    fi

    # Install agent skills (MCP bridge) for the outer agent
    local skills="$REPO_ROOT/the_lab/agent_skills"
    if [ -d "$skills" ]; then
        mkdir -p "$PROJ/.claude/skills"
        cp "$skills/skills/lab_api_mcp.py" "$PROJ/.claude/skills/" 2>/dev/null || true
        cp "$skills/mcp.json" "$PROJ/.mcp.json" 2>/dev/null || true
        cp "$skills/settings.json" "$PROJ/.claude/settings.json" 2>/dev/null || true
    fi

    # Symlink outer PROMPT.md (the optimization instructions for Opus)
    # The project's own PROMPT.md (for the inner agent) is managed separately
    echo "  proj: $PROJ"
}

# ── Commands ─────────────────────────────────────────────────────────────────

cmd_start() {
    ensure_proj
    local port="${1:-9000}"
    local dev="${2:-}"
    echo "Starting The Lab on port $port..."
    echo "  Project: $PROJ"
    echo "  Dashboard: http://localhost:$port"
    echo ""
    cd "$REPO_ROOT"
    if [ "$dev" = "--dev" ]; then
        echo "  Dev mode: auto-reload on code changes"
        exec env THE_LAB_REPO="$PROJ" THE_LAB_NO_SANDBOX=1 THE_LAB_DEV=1 \
            python3 -m uvicorn the_lab.app:app \
            --host 0.0.0.0 --port "$port" --reload \
            --reload-dir the_lab --reload-dir dashboard/src
    else
        exec env THE_LAB_REPO="$PROJ" THE_LAB_NO_SANDBOX=1 python3 -m uvicorn the_lab.app:app \
            --host 0.0.0.0 --port "$port"
    fi
}

cmd_baseline() {
    ensure_proj
    local inner_budget="${1:-5}"
    local timeout="${2:-3600}"
    local port="${3:-9000}"
    local api="http://127.0.0.1:$port/api/v1"

    echo "Establishing ARC baseline (inner_budget=$inner_budget, timeout=${timeout}s)..."
    echo ""

    # Require Lab to be running
    if ! curl -s "$api/backlog" > /dev/null 2>&1; then
        echo "Error: Lab is not running on port $port."
        echo "Start it first:  $0 start $port"
        exit 1
    fi

    # Create baseline idea
    echo "Creating baseline idea..."
    local idea_resp
    idea_resp=$(curl -s -X POST "$api/ideas/new" \
        -H "Content-Type: application/json" \
        -d '{"description": "Baseline: random agent (no optimization)"}')
    local idea_id
    idea_id=$(echo "$idea_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)

    if [ -z "$idea_id" ]; then
        echo "Error: failed to create baseline idea. Response: $idea_resp"
        exit 1
    fi

    curl -s -X POST "$api/ideas/$idea_id/checkout" > /dev/null

    # Create experiment with run_eval_arc.py
    local eval_script="$SCRIPT_DIR/run_eval_arc.py"
    local script_content="#!/bin/bash\nset -euo pipefail\npython $eval_script --model gemma-4-31b --budget $inner_budget --timeout $timeout"
    local exp_resp
    exp_resp=$(curl -s -X POST "$api/ideas/$idea_id/experiments" \
        -H "Content-Type: application/json" \
        -d "{\"description\": \"Baseline eval (random agent, inner_budget=$inner_budget)\", \"script_content\": \"$script_content\", \"tags\": [\"baseline\", \"method-random\"]}")
    local exp_id
    exp_id=$(echo "$exp_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)

    if [ -z "$exp_id" ]; then
        echo "Error: failed to create experiment. Response: $exp_resp"
        exit 1
    fi

    echo "Created idea #$idea_id, experiment #$exp_id"
    echo "Waiting for experiment to finish (timeout: ${timeout}s)..."
    curl -s "$api/wait?experiment_id=$exp_id&timeout=$((timeout + 300))" > /dev/null

    # Get results
    local result
    result=$(curl -s "$api/experiments/$exp_id")
    echo "$result" > "$SCRIPT_DIR/baseline_arc.json"

    python3 -c "
import json
exp = json.load(open('$SCRIPT_DIR/baseline_arc.json'))
metrics = exp.get('metrics', {})
if metrics:
    print()
    print(f'  best_score:            {metrics.get(\"best_score\", \"n/a\")}')
    print(f'  experiments_completed: {metrics.get(\"experiments_completed\", \"n/a\")}')
    print(f'  experiments_failed:    {metrics.get(\"experiments_failed\", \"n/a\")}')
    print(f'  wall_seconds:          {metrics.get(\"wall_seconds\", \"n/a\")}')
    print()
    print('Baseline saved to optimization/baseline_arc.json')
else:
    print('Warning: experiment has no metrics yet. Status:', exp.get('status'))
    print(f'  curl $api/experiments/$exp_id/log?tail=30')
"

    # Conclude
    curl -s -X POST "$api/ideas/$idea_id/conclude" \
        -H "Content-Type: application/json" \
        -d '{"conclusion": "Baseline established with random agent."}' > /dev/null
    echo "Baseline idea #$idea_id concluded."
}

cmd_agent() {
    ensure_proj
    local outer_model="${1:-opus}"
    echo "Launching ARC optimization agent..."
    echo "  Outer model: $outer_model"
    echo "  Working directory: $PROJ"
    echo ""
    cd "$PROJ"
    exec the-lab-agent loop --model "$outer_model"
}

cmd_reset() {
    echo "Resetting ARC project Lab data..."

    if [ -d "$PROJ/.the_lab" ]; then
        # Preserve artifacts symlinks, remove experiments/ideas/notes
        rm -rf "$PROJ/.the_lab/experiments" "$PROJ/.the_lab/worktrees" 2>/dev/null || true
        rm -f "$PROJ/.the_lab/api_stats.json" "$PROJ/.the_lab/task.json" 2>/dev/null || true
        echo "Cleared .the_lab experiment data."
    fi

    # Reset git to clean state
    if [ -d "$PROJ/.git" ]; then
        cd "$PROJ"
        # Remove idea branches
        git branch | grep -v '^\*\|main\|master' | xargs -r git branch -D 2>/dev/null || true
        git checkout main 2>/dev/null || git checkout master 2>/dev/null || true
        cd "$SCRIPT_DIR"
    fi

    ensure_proj
    echo "Done. Clean slate."
}

cmd_cherry_pick() {
    local commit="$1"
    echo "Cherry-picking $commit from arc3_autosolver..."
    cd "$PROJ"
    git cherry-pick "$commit"
    echo "Done. Verify with: git log --oneline -3"
}

# ── Dispatch ─────────────────────────────────────────────────────────────────

case "${1:-help}" in
    start)
        shift
        cmd_start "$@"
        ;;
    baseline)
        shift
        cmd_baseline "$@"
        ;;
    agent)
        shift
        cmd_agent "$@"
        ;;
    reset)
        cmd_reset
        ;;
    cherry-pick)
        shift
        cmd_cherry_pick "$@"
        ;;
    *)
        echo "ARC-AGI-3 Optimization"
        echo ""
        echo "Usage: $0 <command> [args]"
        echo ""
        echo "Commands:"
        echo "  start [port] [--dev]                             Start the Lab dashboard"
        echo "  baseline [inner-budget] [timeout] [port]         Establish baseline (random agent)"
        echo "  agent [outer-model]                              Launch optimization agent (default: opus)"
        echo "  reset                                            Clear experiment data, keep project code"
        echo "  cherry-pick <commit>                             Cherry-pick a winning idea"
        echo ""
        echo "Prerequisites:"
        echo "  1. vLLM serving Gemma 4 31B on port 8008"
        echo "  2. LiteLLM proxy on port 4000 (./model_inference/launch_litellm.sh)"
        echo "  3. ARC game server on port 8001"
        echo ""
        echo "Typical flow:"
        echo "  $0 start                                        # open dashboard at :9000"
        echo "  $0 baseline 5 3600                              # ~1h, random agent baseline"
        echo "  $0 agent opus                                   # opus optimizes inner agent strategy"
        echo "  $0 cherry-pick abc1234                          # save winning PROMPT.md changes"
        ;;
esac
