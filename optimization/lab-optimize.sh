#!/usr/bin/env bash
set -euo pipefail

# The Lab autoresearch optimization wrapper.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJ="$SCRIPT_DIR/proj"
REMOTE="https://github.com/karpathy/autoresearch.git"

if [ -z "${VIRTUAL_ENV:-}" ] && [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    source "$REPO_ROOT/.venv/bin/activate"
fi

ensure_proj() {
    if [ ! -f "$PROJ/train.py" ]; then
        rm -rf "$PROJ"
        git clone "$REMOTE" "$PROJ"
    fi

    mkdir -p "$PROJ/.the_lab/artifacts" "$PROJ/.the_lab/sandbox"
    ln -sf ../../../run_eval.py "$PROJ/.the_lab/artifacts/run_eval.py"
    ln -sf ../PROMPT.md "$PROJ/PROMPT.md"
    printf '{"enabled": false}\n' > "$PROJ/.the_lab/sandbox/config.json"
    touch "$PROJ/.git/info/exclude"
    grep -qxF ".the_lab/" "$PROJ/.git/info/exclude" || printf ".the_lab/\n" >> "$PROJ/.git/info/exclude"
    grep -qxF "PROMPT.md" "$PROJ/.git/info/exclude" || printf "PROMPT.md\n" >> "$PROJ/.git/info/exclude"
    cat > "$PROJ/.the_lab/metric_directions.json" <<'JSON'
{
  "loss": "minimize",
  "val_bpb": "minimize",
  "peak_vram_mb": "minimize",
  "peak_vram_gb": "minimize"
}
JSON
}

cmd_start() {
    ensure_proj
    local port="${1:-9000}"
    echo "Starting The Lab on port $port"
    echo "  Repo: $PROJ"
    echo "  Metric: val_bpb/loss (minimize)"
    cd "$REPO_ROOT"
    exec env THE_LAB_REPO="$PROJ" THE_LAB_NO_SANDBOX=1 python3 -m uvicorn the_lab.app:app \
        --host 0.0.0.0 --port "$port"
}

cmd_baseline() {
    ensure_proj
    local port="${1:-9000}"
    local api="http://127.0.0.1:$port/api/v1"

    if ! curl -s "$api/backlog" > /dev/null 2>&1; then
        echo "Error: Lab is not running on port $port."
        echo "Start it first: $0 start $port"
        exit 1
    fi

    local idea_resp idea_id exp_resp exp_id
    idea_resp=$(curl -s -X POST "$api/ideas/new" \
        -H "Content-Type: application/json" \
        -d '{"description":"Baseline autoresearch train.py"}')
    idea_id=$(echo "$idea_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
    curl -s -X POST "$api/ideas/$idea_id/checkout" > /dev/null

    exp_resp=$(curl -s -X POST "$api/ideas/$idea_id/experiments" \
        -H "Content-Type: application/json" \
        -d '{"description":"Baseline val_bpb from upstream train.py","script_content":"#!/usr/bin/env bash\nset -euo pipefail\npython .the_lab/artifacts/run_eval.py","tags":["baseline","autoresearch"]}')
    exp_id=$(echo "$exp_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
    curl -s -X POST "$api/experiments/$exp_id/start" > /dev/null
    echo "Started baseline idea #$idea_id experiment #$exp_id"
    echo "Waiting for completion..."
    curl -s "$api/wait?experiment_id=$exp_id&timeout=1200"
}

cmd_agent() {
    ensure_proj
    local model="${1:-sonnet}"
    cd "$PROJ"
    exec the-lab-agent loop --model "$model"
}

cmd_reset() {
    rm -rf "$PROJ"
    git clone "$REMOTE" "$PROJ"
    ensure_proj
    echo "Reset optimization/proj to $REMOTE"
}

cmd_cherry_pick() {
    local commit="$1"
    ensure_proj
    cd "$PROJ"
    git cherry-pick "$commit"
}

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
        echo "The Lab autoresearch optimization"
        echo ""
        echo "Usage: $0 <command> [args]"
        echo ""
        echo "Commands:"
        echo "  reset                 Replace optimization/proj with karpathy/autoresearch"
        echo "  start [port]          Start The Lab for optimization/proj"
        echo "  baseline [port]       Run upstream train.py and record val_bpb"
        echo "  agent [model]         Launch optimizer loop"
        echo "  cherry-pick <commit>  Cherry-pick a winning change to this repo"
        ;;
esac
