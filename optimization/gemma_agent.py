#!/usr/bin/env python3
"""Simple agentic loop for models that don't chain tool calls autonomously.

Calls the model via OpenAI chat completions, executes tool calls locally,
feeds results back, and repeats until the model stops calling tools or
max_turns is reached.

Usage:
    python simple_agent.py --prompt "Read PROMPT.md and do the task" --cwd /path/to/project
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

VLLM_BASE = os.environ.get("VLLM_BASE", "http://localhost:8008/v1")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "QuantTrio/gemma-4-31B-it-AWQ")
LAB_API_URL = os.environ.get("THE_LAB_API_URL", "")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "50"))


# ---------------------------------------------------------------------------
# Auto-generate Lab API tools from /openapi.json
# ---------------------------------------------------------------------------

# Subset of Lab endpoints to expose (method, path) → tool_name
LAB_ENDPOINTS = {
    ("get",  "/api/v1/orient"):                    "lab_orient",
    ("get",  "/api/v1/leaderboard/search"):        "lab_leaderboard_search",
    ("get",  "/api/v1/wait"):                      "lab_wait",
    ("post", "/api/v1/ideas/new"):                 "lab_create_idea",
    ("get",  "/api/v1/ideas/{idea_id}"):           "lab_get_idea",
    ("post", "/api/v1/ideas/{idea_id}/conclude"):  "lab_conclude_idea",
    ("post", "/api/v1/ideas/{idea_id}/note"):      "lab_add_note",
    ("post", "/api/v1/ideas/{idea_id}/experiments"): "lab_create_experiment",
    ("get",  "/api/v1/experiments"):               "lab_list_experiments",
    ("get",  "/api/v1/experiments/log"):           "lab_get_failed_logs",
    ("get",  "/api/v1/experiments/tags"):          "lab_list_tags",
    ("get",  "/api/v1/experiments/{exp_ref}"):     "lab_get_experiment",
    ("get",  "/api/v1/experiments/{exp_ref}/log"): "lab_get_experiment_log",
}

_lab_tool_meta: dict = {}  # tool_name → {method, path_template, path_params, query_params, body_params}


def _resolve_ref(ref: str, spec: dict):
    parts = ref.lstrip("#/").split("/")
    node = spec
    for p in parts:
        node = node[p]
    return node


def _build_lab_tools(api_url: str) -> list[dict]:
    """Fetch /openapi.json and build OpenAI-format tool definitions."""
    base = api_url.rstrip("/")
    for suffix in ("/api/v1", "/api/v1/"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    try:
        req = urllib.request.Request(f"{base}/openapi.json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            spec = json.loads(resp.read())
    except Exception as e:
        print(f"  Warning: could not fetch openapi.json: {e}", file=sys.stderr)
        return []

    tools = []
    for path, methods in spec.get("paths", {}).items():
        for method, detail in methods.items():
            key = (method, path)
            if key not in LAB_ENDPOINTS:
                continue
            tool_name = LAB_ENDPOINTS[key]

            path_params, query_params, body_params = set(), set(), set()
            properties, required = {}, []

            for param in detail.get("parameters", []):
                name = param["name"]
                schema = param.get("schema", {"type": "string"})
                if "$ref" in schema:
                    schema = _resolve_ref(schema["$ref"], spec)
                prop = {k: schema[k] for k in ("type", "default", "enum") if k in schema}
                desc = param.get("description", "")
                if desc:
                    prop["description"] = desc
                properties[name] = prop
                if param.get("in") == "path":
                    path_params.add(name)
                    required.append(name)
                elif param.get("in") == "query":
                    query_params.add(name)
                    if param.get("required"):
                        required.append(name)

            body_spec = detail.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
            if "$ref" in body_spec:
                body_spec = _resolve_ref(body_spec["$ref"], spec)
            for pname, pschema in body_spec.get("properties", {}).items():
                if "$ref" in pschema:
                    pschema = _resolve_ref(pschema["$ref"], spec)
                prop = {k: pschema[k] for k in ("type", "default", "enum", "description") if k in pschema}
                properties[pname] = prop
                body_params.add(pname)
            for rname in body_spec.get("required", []):
                if rname not in required:
                    required.append(rname)

            desc = detail.get("summary", detail.get("description", tool_name))
            if len(desc) > 300:
                desc = desc[:297] + "..."

            tools.append({
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": desc,
                    "parameters": {"type": "object", "properties": properties, "required": required} if properties else {"type": "object"},
                },
            })
            _lab_tool_meta[tool_name] = {
                "method": method.upper(),
                "path_template": path,
                "path_params": path_params,
                "query_params": query_params,
                "body_params": body_params,
            }

    print(f"  Loaded {len(tools)} Lab API tools", file=sys.stderr)
    return tools


def _execute_lab_tool(tool_name: str, args: dict, api_url: str) -> str:
    """Execute a Lab API tool call via HTTP."""
    meta = _lab_tool_meta[tool_name]
    path = meta["path_template"]
    for p in meta["path_params"]:
        path = path.replace(f"{{{p}}}", str(args.get(p, "")))

    base = api_url.rstrip("/")
    for suffix in ("/api/v1", "/api/v1/"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    url = f"{base}{path}"

    query = {p: args[p] for p in meta["query_params"] if p in args and args[p] is not None}
    if query:
        url += "?" + "&".join(f"{k}={v}" for k, v in query.items())

    body_data = None
    if meta["body_params"]:
        body_obj = {p: args[p] for p in meta["body_params"] if p in args}
        body_data = json.dumps(body_obj).encode()

    headers = {"Content-Type": "application/json"} if body_data else {}
    req = urllib.request.Request(url, data=body_data, headers=headers, method=meta["method"])
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.read().decode()[:8000]
    except Exception as e:
        return json.dumps({"error": str(e)})


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a bash command and return stdout+stderr",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The bash command to run"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file and return its contents",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path to read"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file (creates or overwrites)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
]


def _init_tools():
    """Build the full tool list: base tools + Lab API tools (if available)."""
    all_tools = list(TOOLS)
    if LAB_API_URL:
        all_tools.extend(_build_lab_tools(LAB_API_URL))
    return all_tools

_ALL_TOOLS: list[dict] = []  # populated in main()


def call_model(messages, max_tokens=4096):
    body = json.dumps({
        "model": VLLM_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "tools": _ALL_TOOLS,
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(
        f"{VLLM_BASE}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def execute_tool(name, args, cwd):
    # Lab API tools
    if name in _lab_tool_meta:
        print(f"  [lab] {name}({json.dumps(args)[:100]})", file=sys.stderr)
        return _execute_lab_tool(name, args, LAB_API_URL)
    if name == "bash":
        cmd = args.get("command", "echo 'no command'")
        print(f"  [bash] {cmd[:120]}", file=sys.stderr)
        result = subprocess.run(
            ["bash", "-c", cmd], cwd=cwd,
            capture_output=True, text=True, timeout=300,
        )
        output = result.stdout + result.stderr
        return output[:8000] if output else "(no output)"
    elif name == "read_file":
        path = Path(cwd) / args.get("path", "")
        try:
            content = path.read_text()
            return content[:8000]
        except Exception as e:
            return f"Error: {e}"
    elif name == "write_file":
        path = Path(cwd) / args.get("path", "")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content", ""))
            return f"Written {len(args.get('content', ''))} chars to {path}"
        except Exception as e:
            return f"Error: {e}"
    else:
        return f"Unknown tool: {name}"


def run_agent(prompt, cwd, system_prompt=None, max_turns=None):
    max_turns = max_turns or MAX_TURNS
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    for turn in range(max_turns):
        print(f"[turn {turn + 1}/{max_turns}]", file=sys.stderr)
        resp = call_model(messages)
        choice = resp["choices"][0]
        msg = choice["message"]
        messages.append(msg)

        # Check for tool calls
        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            # Model produced text only — done
            text = msg.get("content", "")
            if text:
                print(f"  [text] {text[:200]}", file=sys.stderr)
            print(f"Agent finished after {turn + 1} turns (no more tool calls)", file=sys.stderr)
            return text

        # Execute each tool call
        for tc in tool_calls:
            fn = tc["function"]
            try:
                args = json.loads(fn["arguments"])
            except (json.JSONDecodeError, TypeError):
                args = {}
            result = execute_tool(fn["name"], args, cwd)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    print(f"Agent hit max turns ({max_turns})", file=sys.stderr)
    return messages[-1].get("content", "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", "-p", required=True)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--system-prompt", default=None)
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS)
    args = parser.parse_args()

    global _ALL_TOOLS
    _ALL_TOOLS = _init_tools()
    print(f"Tools: {len(_ALL_TOOLS)} ({len(TOOLS)} base + {len(_ALL_TOOLS) - len(TOOLS)} lab)", file=sys.stderr)

    result = run_agent(args.prompt, args.cwd, args.system_prompt, args.max_turns)
    print(result)


if __name__ == "__main__":
    main()
