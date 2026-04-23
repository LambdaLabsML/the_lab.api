"""Operational endpoints: task, config, sandbox, stats, chat, debug."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from ..deps import (
    store,
    api_stats,
    REPO_DIR,
    _INTERNAL_META_KEYS,
    _read_task,
    _write_task,
    _read_metric_directions,
)
from ..sandbox import (
    list_observed_accesses,
    load_sandbox_config,
    save_sandbox_config,
    sandbox_capabilities,
)
from ..schemas import (
    TaskRequest,
    SandboxConfigRequest,
    ChatRequest,
)
from ..store import Store

logger = logging.getLogger(__name__)

router = APIRouter()


# --- Instructions ---

_PROMPT_API_PATH = Path(__file__).parent.parent / "PROMPT_api.md"


@router.get("/api/v1/instructions")
def get_instructions():
    """Return the combined project instructions (PROMPT.md + PROMPT_api.md).

    PROMPT.md is read from the repo root (project-specific task description).
    PROMPT_api.md ships with the_lab and documents all available API tools.

    Use this at the start of a session (or each loop iteration) to get a
    fresh copy of the current task and API reference.

    Example:
        GET /api/v1/instructions
        -> {"content": "# Problem\\n\\n...\\n\\n---\\n\\n# Lab API\\n\\n..."}
    """
    parts = []
    prompt_path = REPO_DIR / "PROMPT.md"
    if prompt_path.exists():
        parts.append(f"# Problem\n\n{prompt_path.read_text().strip()}")
    if _PROMPT_API_PATH.exists():
        parts.append(_PROMPT_API_PATH.read_text().strip())
    return {"content": "\n\n---\n\n".join(parts)}


# --- Current task ---

@router.get("/api/v1/task")
def get_task():
    """Get the current task (default direction when no ideas are suggested).

    Returns the task text and when it was last updated, or ``null`` if no
    task is set.

    Example:
        GET /api/v1/task
        -> {"text": "Focus on improving group_accuracy on problem 066", "updated_at": "..."}
    """
    return _read_task()


@router.put("/api/v1/task")
def set_task(req: TaskRequest):
    """Set or update the current task.

    The task acts as a standing directive for agents when there are no
    suggested ideas to adopt. Set an empty ``text`` to clear it.

    Example:
        PUT /api/v1/task {"text": "Explore cascade strategies with swarm_size=7"}
        -> {"text": "Explore cascade strategies...", "updated_at": "..."}
    """
    task_path = REPO_DIR / ".the_lab" / "task.json"
    if not req.text.strip():
        if task_path.exists():
            task_path.unlink()
            # Invalidate caches that embed the task (list_ideas, backlog).
            store._version += 1
        return None
    return _write_task(req.text.strip())


# --- Dashboard config ---

@router.get("/api/v1/config")
def get_dashboard_config():
    """Dashboard UI defaults for this project.

    Reads ``.the_lab/dashboard.json`` from the repo root. Any key present
    in this file is used as the default for new users (before localStorage
    overrides). Missing file or missing keys -> empty defaults.

    Supported keys: ``tagFilters``, ``tagFilterMode``, ``selectedMetric``,
    ``colorMode``, ``improvementsOnly``, ``reverseTime``, ``showAbandoned``,
    ``showConcluded``, ``showRunning``.

    Example ``.the_lab/dashboard.json``:
        {"tagFilters": ["held-out"], "selectedMetric": "accuracy_per_mtoken"}
    """
    config_path = REPO_DIR / ".the_lab" / "dashboard.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# --- Metric directions ---

@router.get("/api/v1/config/metric-directions")
def get_metric_directions():
    """Get custom metric direction overrides.

    Returns a dict mapping metric key → "minimize" | "maximize".
    These override the default pattern-based inference.
    Missing file returns an empty dict (all defaults apply).

    Example:
        GET /api/v1/config/metric-directions
        -> {"my_custom_score": "maximize", "some_loss": "minimize"}
    """
    return _read_metric_directions()


@router.post("/api/v1/config/metric-directions")
def set_metric_directions(directions: dict):
    """Set custom metric direction overrides.

    Keys are metric names, values must be "minimize" or "maximize".
    Overwrites the full config (use GET first to merge with existing).

    Example:
        POST /api/v1/config/metric-directions
        {"ttft_p50_ms": "minimize", "custom_accuracy": "maximize"}
        -> {"ttft_p50_ms": "minimize", "custom_accuracy": "maximize"}
    """
    for k, v in directions.items():
        if v not in ("minimize", "maximize"):
            raise HTTPException(400, f"Invalid direction '{v}' for key '{k}': must be 'minimize' or 'maximize'")
    path = REPO_DIR / ".the_lab" / "metric_directions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(directions, indent=2) + "\n")
    return directions


# --- Sandbox ---

@router.get("/api/v1/sandbox")
def get_sandbox_state():
    config = load_sandbox_config(REPO_DIR)
    capabilities = sandbox_capabilities()
    observed = list_observed_accesses(REPO_DIR)
    return {
        **config,
        "capabilities": capabilities,
        "observed": observed,
    }


@router.put("/api/v1/sandbox")
def update_sandbox_state(req: SandboxConfigRequest):
    config = save_sandbox_config(
        REPO_DIR,
        {
            "enabled": req.enabled,
            "allowlist": req.allowlist,
            "denylist": req.denylist,
            "file_rw": req.file_rw,
            "file_ro": req.file_ro,
        },
    )
    capabilities = sandbox_capabilities()
    observed = list_observed_accesses(REPO_DIR)
    return {
        **config,
        "capabilities": capabilities,
        "observed": observed,
    }


# --- API Stats ---

@router.get("/api/v1/stats")
def get_api_stats(
    pattern_length: int = Query(default=2, ge=2, le=5, description="Length of call patterns to return (2=pairs, 3=triples, etc.)"),
):
    """Get API endpoint usage statistics.

    Returns per-endpoint call counts and the most common sequential call
    patterns. Use ``pattern_length`` to control the sequence length:
    2 for pairs (A->B), 3 for triples (A->B->C), up to 5.

    Example:
        GET /api/v1/stats?pattern_length=3
        -> {"total_calls": 5048, "pattern_length": 3,
            "calls": [{"endpoint": "GET /api/v1/digest", "count": 420}, ...],
            "patterns": [{"sequence": "digest -> suggested -> new", "count": 80}, ...]}
    """
    result = api_stats.get_stats(pattern_length=pattern_length)
    result["history"] = api_stats.get_history(limit=100)
    return result


@router.post("/api/v1/stats/import")
def import_api_stats(data: dict):
    """Import/merge external stats (e.g. from backfill script).

    Accepts ``{"calls": {...}, "patterns": {...}, "patterns_by_n": {"2": {...}, "3": {...}}}``
    and merges them into the current running stats.
    """
    api_stats.merge(
        data.get("calls", {}),
        data.get("patterns", {}),
        patterns_by_n=data.get("patterns_by_n"),
        reset=bool(data.get("reset")),
    )
    api_stats.flush()
    return {"status": "ok", "total_calls": api_stats.get_stats()["total_calls"]}


# --- Debug chart test page ---

_CHART_TEST_HTML = """\
<!DOCTYPE html>
<html><head><title>Chart Test</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
</head><body style="background:#0d1117;color:#c9d1d9;font-family:monospace;padding:20px">
<h3>Chart.js Test</h3>
<div id="status">Loading chart-data...</div>
<div style="height:300px;background:#161b22;border:1px solid #30363d;padding:10px">
  <canvas id="test-chart"></canvas>
</div>
<script>
fetch('/api/v1/chart-data').then(r=>r.json()).then(d=>{
  const exps=d.experiments.filter(e=>e.metrics&&e.metrics.accuracy_per_mtoken!==undefined);
  exps.sort((a,b)=>(a.finished_at||'').localeCompare(b.finished_at||''));
  document.getElementById('status').textContent=
    'Loaded '+exps.length+' experiments with accuracy_per_mtoken';
  new Chart(document.getElementById('test-chart'),{
    type:'line',
    data:{
      labels:exps.map(e=>'exp/'+e.id),
      datasets:[{label:'accuracy_per_mtoken',
        data:exps.map(e=>e.metrics.accuracy_per_mtoken),
        borderColor:'#58a6ff',pointRadius:4,pointBackgroundColor:'#58a6ff',tension:0}]
    },
    options:{responsive:true,maintainAspectRatio:false,
      scales:{y:{ticks:{color:'#8b949e'}},x:{ticks:{color:'#8b949e',maxRotation:0,autoSkip:true}}}}
  });
}).catch(e=>{document.getElementById('status').textContent='ERROR: '+e.message});
</script></body></html>
"""


@router.get("/debug/chart-test", response_class=HTMLResponse, include_in_schema=False)
def chart_test_page():
    """Minimal Chart.js test page -- for debugging chart rendering issues."""
    return _CHART_TEST_HTML


# --- Chat ---

_CHAT_SYSTEM_PROMPT = """\
You are a research assistant for a project tracked in The Lab. \
Below is the complete project state. Cite specific idea/experiment IDs, \
highlight config differences, and be precise with numbers.\
"""


def _build_chat_context() -> str:
    """Serialize the full project state into structured text for an LLM."""
    all_ideas = store.list_ideas()
    all_exps = store.list_all_experiments()
    exps_by_idea: dict[int, list[dict]] = {}
    for exp in all_exps:
        exps_by_idea.setdefault(exp["idea_id"], []).append(exp)
    lines: list[str] = []
    lines.append(f"# Project: {len(all_ideas)} ideas, {len(all_exps)} experiments\n")
    for idea in all_ideas:
        iid = idea["id"]
        parents = ", ".join(f"#{p}" for p in idea.get("parent_ids", [])) or "root"
        lines.append(f"## Idea #{iid} [{idea.get('status')}] \"{idea.get('description', '')}\"")
        lines.append(f"  Parents: {parents}")
        if idea.get("conclusion"):
            lines.append(f"  Conclusion: \"{idea['conclusion']}\"")
        for note in store.get_notes(iid, levels=Store.ALL_LEVELS):
            lines.append(f"  [{note.get('level', 'observation')}] \"{note['text']}\"")
        for exp in sorted(exps_by_idea.get(iid, []), key=lambda e: e.get("created_at", "")):
            line = f"  #{exp['id']} [{exp.get('status')}] \"{exp.get('description', '')}\""
            if exp.get("metrics"):
                line += f"  metrics: {{{', '.join(f'{k}: {v}' for k, v in exp['metrics'].items() if v is not None)}}}"
            meta = {k: v for k, v in (exp.get("meta") or {}).items() if k not in _INTERNAL_META_KEYS}
            if meta:
                line += f"  meta: {{{', '.join(f'{k}: {v}' for k, v in meta.items())}}}"
            lines.append(line)
        lines.append("")
    return "\n".join(lines)


@router.get("/api/v1/chat/status")
def chat_status():
    """Check whether the chat feature is available."""
    return {"available": bool(os.environ.get("ANTHROPIC_API_KEY"))}


@router.post("/api/v1/chat")
async def chat(req: ChatRequest):
    """Ask a question about the research project using Claude. Streams SSE."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(501, "ANTHROPIC_API_KEY not set")
    try:
        import anthropic
    except ImportError:
        raise HTTPException(501, "anthropic package not installed")
    context = _build_chat_context()
    system = _CHAT_SYSTEM_PROMPT + "\n---\n\n" + context
    client = anthropic.Anthropic(api_key=api_key)

    async def _stream():
        try:
            with client.messages.stream(
                model="claude-sonnet-4-20250514", max_tokens=4096,
                system=system, messages=req.messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'type': 'text', 'text': text})}\n\n"
            yield 'data: {"type": "done"}\n\n'
        except Exception as e:
            logger.exception("Chat stream error")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")
