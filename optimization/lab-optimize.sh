#!/usr/bin/env bash
set -euo pipefail

# The Lab v4 Self-Optimization
#
# Usage:
#   ./optimization/lab-optimize.sh baseline [eval-model] [budget]
#   ./optimization/lab-optimize.sh start [port]
#   ./optimization/lab-optimize.sh agent [outer-model] [eval-model] [budget]
#   ./optimization/lab-optimize.sh cherry-pick <commit>

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJ="$SCRIPT_DIR/proj"

# Activate the repo's venv if not already active
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    source "$REPO_ROOT/.venv/bin/activate"
fi

# ── Setup proj/ if it doesn't exist ──────────────────────────────────────────

ensure_proj() {
    if [ -f "$PROJ/the_lab/app.py" ]; then
        return
    fi
    echo "Setting up optimization/proj/..."
    mkdir -p "$PROJ/.the_lab/artifacts"

    # Copy .git and checkout optim branch
    cp -r "$REPO_ROOT/.git" "$PROJ/.git"
    cd "$PROJ"
    git checkout -b optim 2>/dev/null || git checkout optim

    # Copy API code
    cp -r "$REPO_ROOT/the_lab" the_lab
    rm -rf the_lab/static the_lab/__pycache__
    find the_lab -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
    cp "$REPO_ROOT/pyproject.toml" pyproject.toml

    # Symlinks to committed static files
    ln -sf ../test_project/PROMPT_problem.md PROMPT_problem.md
    ln -sf ../../../run_eval.py .the_lab/artifacts/run_eval.py
    ln -sf ../../../test_project .the_lab/artifacts/test_project

    # Install pre-commit hook
    cat > .git/hooks/pre-commit << 'HOOK'
#!/bin/bash
if git diff --cached --name-only | grep -q '^\.the_lab/' ; then
    echo "ERROR: .the_lab/ files staged for commit." >&2
    exit 1
fi
HOOK
    chmod +x .git/hooks/pre-commit

    # Disable sandbox for optimization experiments
    mkdir -p .the_lab/sandbox
    echo '{"enabled": false}' > .the_lab/sandbox/config.json

    git add -A
    git commit -m "Initial optimization project" 2>/dev/null || true
    echo "Done. proj/ ready on branch 'optim'."
    cd "$SCRIPT_DIR"
}

# ── Commands ─────────────────────────────────────────────────────────────────

cmd_baseline() {
    ensure_proj
    local model="${1:-haiku}"
    local budget="${2:-10}"
    local port="${3:-9000}"
    local eval_agent="${4:-claude}"
    local api="http://127.0.0.1:$port/api/v1"
    echo "Establishing baseline (model=$model, budget=$budget)..."
    echo "This will launch an agent against the test project (~\$1-3, ~5 min)."
    echo ""
    cd "$PROJ"
    local output_file="$SCRIPT_DIR/baseline_full.json"

    # Start outer Lab temporarily if not already running
    local lab_started=false
    if ! curl -s "$api/backlog" > /dev/null 2>&1; then
        echo "Starting outer Lab on port $port..."
        cd "$REPO_ROOT"
        THE_LAB_REPO="$PROJ" THE_LAB_NO_SANDBOX=1 \
            python3 -m uvicorn the_lab.app:app --host 0.0.0.0 --port "$port" --log-level warning &
        local lab_pid=$!
        lab_started=true
        sleep 2
        cd "$PROJ"
    fi

    # Create baseline idea via the API
    echo "Creating baseline idea..."
    local idea_resp
    idea_resp=$(curl -s -X POST "$api/ideas/new" \
        -H "Content-Type: application/json" \
        -d "{\"description\": \"Baseline: unmodified API (model=$model, budget=$budget)\"}")
    local idea_id
    idea_id=$(echo "$idea_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)

    if [ -z "$idea_id" ]; then
        echo "Error: failed to create baseline idea. Response: $idea_resp"
        [ "$lab_started" = true ] && kill "$lab_pid" 2>/dev/null
        exit 1
    fi

    # Checkout the idea branch
    curl -s -X POST "$api/ideas/$idea_id/checkout" > /dev/null

    # Create experiment
    local run_eval="$SCRIPT_DIR/run_eval.py"
    local script_content="#!/bin/bash\nset -euo pipefail\npython $run_eval --agent $eval_agent --model $model --budget $budget --tests t1,t2,t3,t4,t5,t6,t7,t8"
    local exp_resp
    exp_resp=$(curl -s -X POST "$api/ideas/$idea_id/experiments" \
        -H "Content-Type: application/json" \
        -d "{\"description\": \"Baseline eval (model=$model, budget=$budget)\", \"script_content\": \"$script_content\", \"tags\": [\"baseline\", \"$model\", \"budget-$budget\"]}")
    local exp_id
    exp_id=$(echo "$exp_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)

    if [ -z "$exp_id" ]; then
        echo "Error: failed to create experiment. Response: $exp_resp"
        [ "$lab_started" = true ] && kill "$lab_pid" 2>/dev/null
        exit 1
    fi

    echo "Created idea #$idea_id, experiment #$exp_id"
    echo "Starting experiment..."

    # Start and wait
    curl -s -X POST "$api/experiments/$exp_id/start" > /dev/null
    echo "Waiting for experiment to finish..."
    curl -s "$api/wait?experiment_id=$exp_id&timeout=900" > /dev/null

    # Get results
    local result
    result=$(curl -s "$api/experiments/$exp_id")
    echo "$result" > "$output_file"

    # Save baseline.json from the experiment metrics
    python3 -c "
import json
exp = json.load(open('$output_file'))
metrics = exp.get('metrics', {})
if metrics:
    json.dump(metrics, open('.the_lab/artifacts/baseline.json', 'w'), indent=2)
    print()
    print(f'  api_effectiveness: {metrics.get(\"api_effectiveness\", \"n/a\")}')
    for i in range(1, 9):
        k = f't{i}.score'
        print(f'  {k}:' + ' ' * (17-len(k)) + f'{metrics.get(k, \"n/a\")}')
    print(f'  total_api_calls:   {metrics.get(\"total_api_calls\", \"n/a\")}')
    print(f'  total_cost:        \${metrics.get(\"total_cost\", 0)}')
    print()
    print('Baseline saved to .the_lab/artifacts/baseline.json')
else:
    print('Warning: experiment has no metrics yet. Status:', exp.get('status'))
    print('Check experiment log:')
    print(f'  curl $api/experiments/$exp_id/log?tail=30')
"

    # Conclude the baseline idea
    curl -s -X POST "$api/ideas/$idea_id/conclude" \
        -H "Content-Type: application/json" \
        -d '{"conclusion": "Baseline established. All future scores are relative to this."}' > /dev/null
    echo "Baseline idea #$idea_id concluded."

    [ "$lab_started" = true ] && kill "$lab_pid" 2>/dev/null
}

cmd_start() {
    ensure_proj
    local port="${1:-9000}"
    local dev="${2:-}"
    echo "Starting The Lab on port $port..."
    echo "  Using parent repo's Lab implementation (not proj/the_lab/)"
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

cmd_agent() {
    ensure_proj
    local outer_model="${1:-sonnet}"
    local eval_model="${2:-haiku}"
    local budget="${3:-10}"
    echo "Launching optimization agent..."
    echo "  Outer model (optimization): $outer_model"
    echo "  Eval model (inner agent):   $eval_model"
    echo "  Budget per eval:            $budget experiments"
    echo "  Working directory:           $PROJ"
    echo ""
    cd "$PROJ"
    exec the-lab-agent PROMPT_problem.md --model "$outer_model" --no-sandbox
}

cmd_reset() {
    echo "Resetting optimization/proj/..."

    # Regenerate test fixtures if seed_fixture.py exists
    local seed="$SCRIPT_DIR/tests/seed_fixture.py"
    if [ -f "$seed" ]; then
        echo "Regenerating test fixtures..."
        python3 "$seed"
    fi

    if [ -d "$PROJ" ]; then
        # Git objects can have read-only permissions
        chmod -R u+w "$PROJ/.git" 2>/dev/null || true
        # NFS stale handles — retry with force
        rm -rf "$PROJ" 2>/dev/null || true
        if [ -d "$PROJ" ]; then
            sleep 2
            rm -rf "$PROJ" 2>/dev/null || true
        fi
        if [ -d "$PROJ" ]; then
            echo "Warning: could not fully remove proj/ (NFS stale handles)."
            echo "Cleaning what we can..."
            find "$PROJ" -not -name '.nfs*' -delete 2>/dev/null || true
            rm -rf "$PROJ/.git" "$PROJ/the_lab" "$PROJ/.the_lab" "$PROJ/PROMPT_problem.md" "$PROJ/pyproject.toml" 2>/dev/null || true
        fi
    fi
    ensure_proj

    echo "Done. Clean slate from parent repo."
}

cmd_cherry_pick() {
    local commit="$1"
    echo "Cherry-picking $commit to main..."
    cd "$REPO_ROOT"
    git cherry-pick "$commit"
    echo "Done. Verify with: git log --oneline -3"
}

# ── Dispatch ─────────────────────────────────────────────────────────────────

case "${1:-help}" in
    baseline)
        shift
        cmd_baseline "$@"
        ;;
    start)
        shift
        cmd_start "$@"
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
        echo "The Lab v4 Self-Optimization"
        echo ""
        echo "Usage: $0 <command> [args]"
        echo ""
        echo "Commands:"
        echo "  baseline [model] [budget] [port] [agent]        Establish baseline (agent: claude|codex)"
        echo "  start [port] [--dev]                             Start the Lab dashboard (--dev = auto-reload)"
        echo "  agent [outer-model] [eval-model] [budget]       Launch optimization agent"
        echo "                                                    outer-model: who optimizes the API (default: sonnet)"
        echo "                                                    eval-model: who runs the test project (default: haiku)"
        echo "                                                    budget: experiments per eval (default: 10)"
        echo "  reset                                            Wipe proj/ data, branches, refresh code"
        echo "  cherry-pick <commit>                            Cherry-pick a winner to main"
        echo ""
        echo "Typical flow:"
        echo "  $0 baseline                                     # ~5 min, ~\$1-3"
        echo "  $0 start                                        # open dashboard at :9000"
        echo "  $0 agent opus haiku 10                          # opus optimizes, haiku evaluates"
        echo "  $0 cherry-pick abc1234                          # merge winners"
        ;;
esac
