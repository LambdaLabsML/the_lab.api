#!/usr/bin/env python3
"""OpenAPI-to-MCP bridge: auto-generates MCP tools from the Lab API's /openapi.json.

Zero dependencies. Reads THE_LAB_API_URL from the environment (set by run_eval.py).
On startup, fetches the OpenAPI spec, builds MCP tool definitions, and proxies
tool calls as HTTP requests to the Lab API.
"""
import json
import os
import sys
import urllib.request
import urllib.parse

# ── Configuration ─────────────────────────────────────────────────────────────

API_BASE = os.environ.get("THE_LAB_API_URL", "http://localhost:8000/api/v1")

# Only expose these paths as MCP tools (keeps system prompt lean).
# Format: (method, path) → tool_name override (or None for auto-name).
INCLUDE = {
    ("get",  "/api/v1/instructions"):              "get_instructions",
    ("get",  "/api/v1/orient"):                    "orient",
    ("get",  "/api/v1/leaderboard/search"):        "leaderboard_search",
    ("get",  "/api/v1/wait"):                      "wait_for_experiment",
    ("post", "/api/v1/ideas/new"):                 "create_idea",
    ("get",  "/api/v1/ideas"):                     "list_ideas",
    ("get",  "/api/v1/ideas/search"):              "search_ideas",
    ("get",  "/api/v1/ideas/{idea_id}"):           "get_idea",
    ("post", "/api/v1/ideas/{idea_id}/checkout"):  "checkout_idea",
    ("post", "/api/v1/ideas/{idea_id}/conclude"):  "conclude_idea",
    ("post", "/api/v1/ideas/{idea_id}/abandon"):   "abandon_idea",
    ("post", "/api/v1/ideas/{idea_id}/adopt"):     "adopt_idea",
    ("post", "/api/v1/ideas/{idea_id}/note"):      "add_note",
    ("get",  "/api/v1/ideas/{idea_id}/notes"):     "list_notes",
    ("post", "/api/v1/ideas/{idea_id}/experiments"): "create_experiment",
    ("get",  "/api/v1/experiments"):               "list_experiments",
    ("get",  "/api/v1/experiments/log"):           "get_failed_logs",
    ("get",  "/api/v1/experiments/tags"):          "list_tags",
    ("post", "/api/v1/experiments/tags/rename"):   "rename_tag",
    ("patch", "/api/v1/experiments/{exp_ref}/tags"): "update_experiment_tags",
    ("post", "/api/v1/experiments/tags/batch"):    "batch_update_tags",
    ("get",  "/api/v1/experiments/{exp_ref}"):     "get_experiment",
    ("get",  "/api/v1/experiments/{exp_ref}/log"): "get_experiment_log",
    ("post", "/api/v1/experiments/{exp_ref}/start"):  "start_experiment",
    ("post", "/api/v1/experiments/{exp_ref}/cancel"): "cancel_experiment",
    ("post", "/api/v1/experiments/{exp_ref}/rerun"):  "rerun_experiment",
    ("get",  "/api/v1/leaderboard"):               "leaderboard",
    # Notifications + inter-agent messaging
    ("get",  "/api/v1/notifications"):             "get_notifications",
    ("get",  "/api/v1/agents"):                    "list_agents",
    ("post", "/api/v1/messages"):                  "send_message",
    ("get",  "/api/v1/messages"):                  "list_messages",
    ("post", "/api/v1/messages/{msg_id}/read"):    "mark_message_read",
    ("post", "/api/v1/messages/read_all"):         "mark_all_messages_read",
}

# ── OpenAPI parsing ───────────────────────────────────────────────────────────

def fetch_openapi_spec():
    """Fetch /openapi.json from the Lab API."""
    # Derive base URL (strip /api/v1 suffix)
    base = API_BASE
    for suffix in ("/api/v1", "/api/v1/"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    url = f"{base}/openapi.json"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def resolve_ref(ref, spec):
    """Resolve a JSON $ref pointer."""
    parts = ref.lstrip("#/").split("/")
    node = spec
    for p in parts:
        node = node[p]
    return node


def flatten_optional(schema, spec):
    """Collapse Pydantic-style ``T | None`` schemas to ``T``.

    Pydantic emits ``Optional[T]`` as ``{"anyOf": [<T>, {"type": "null"}]}``,
    which leaves the property without a ``type`` after our prop builder copies
    only the well-known keys. The calling LLM then has no schema to work
    against and often stringifies dicts/lists, which the proxy then re-encodes
    on top — the agent sees ``"meta"`` arrive as a JSON string. Picking the
    first non-null branch (resolving any inner $ref) restores the type info.
    """
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            if branch.get("type") == "null":
                continue
            if "$ref" in branch:
                branch = resolve_ref(branch["$ref"], spec)
            return branch
    if "$ref" in schema:
        return resolve_ref(schema["$ref"], spec)
    return schema


def build_tools(spec):
    """Build MCP tool definitions and metadata from the OpenAPI spec."""
    tools = []       # MCP tool definitions (for tools/list)
    tool_meta = {}   # tool_name → {method, path_template, path_params, query_params, body_params}

    for path, methods in spec.get("paths", {}).items():
        for method, detail in methods.items():
            key = (method, path)
            if key not in INCLUDE:
                continue
            tool_name = INCLUDE[key]

            # Separate path params from query params
            path_params = set()
            query_params = set()
            properties = {}
            required = []

            for param in detail.get("parameters", []):
                name = param["name"]
                schema = param.get("schema", {"type": "string"})
                schema = flatten_optional(schema, spec)

                prop = {}
                for k in ("type", "items", "default", "enum"):
                    if k in schema:
                        prop[k] = schema[k]
                desc = param.get("description", "")
                if desc:
                    prop["description"] = desc

                properties[name] = prop

                if param.get("in") == "path":
                    path_params.add(name)
                    required.append(name)
                elif param.get("in") == "query":
                    query_params.add(name)
                    if param.get("required", False):
                        required.append(name)

            # Request body fields
            body_params = set()
            body_spec = detail.get("requestBody", {})
            if body_spec:
                content = body_spec.get("content", {})
                json_schema = content.get("application/json", {}).get("schema", {})
                if "$ref" in json_schema:
                    json_schema = resolve_ref(json_schema["$ref"], spec)
                for prop_name, prop_schema in json_schema.get("properties", {}).items():
                    prop_schema = flatten_optional(prop_schema, spec)
                    prop = {}
                    for k in ("type", "items", "default", "enum", "description", "additionalProperties"):
                        if k in prop_schema:
                            prop[k] = prop_schema[k]
                    properties[prop_name] = prop
                    body_params.add(prop_name)
                for req_name in json_schema.get("required", []):
                    if req_name not in required:
                        required.append(req_name)

            # Build description from OpenAPI
            desc = detail.get("description", detail.get("summary", tool_name))
            # Truncate long descriptions to save system prompt tokens
            if len(desc) > 300:
                desc = desc[:297] + "..."

            input_schema = {"type": "object", "properties": properties}
            if required:
                input_schema["required"] = required

            tools.append({
                "name": tool_name,
                "description": desc,
                "inputSchema": input_schema,
            })

            tool_meta[tool_name] = {
                "method": method.upper(),
                "path_template": path,
                "path_params": path_params,
                "query_params": query_params,
                "body_params": body_params,
            }

    return tools, tool_meta


# ── HTTP proxy ────────────────────────────────────────────────────────────────

def proxy_call(tool_name, arguments, tool_meta):
    """Execute an HTTP request to the Lab API and return the response text."""
    meta = tool_meta[tool_name]
    method = meta["method"]
    path = meta["path_template"]

    # Substitute path parameters
    for p in meta["path_params"]:
        val = arguments.get(p, "")
        path = path.replace(f"{{{p}}}", str(val))

    # Build URL with query parameters
    base = API_BASE
    for suffix in ("/api/v1", "/api/v1/"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    url = f"{base}{path}"

    query = {}
    for p in meta["query_params"]:
        if p in arguments and arguments[p] is not None:
            query[p] = arguments[p]
    if query:
        url += "?" + urllib.parse.urlencode(query)

    # Build body from body parameters. Some MCP clients stringify nested
    # dict/list arguments when the schema is fuzzy — undo that here so a
    # JSON string arriving for an object/array param is parsed back before
    # we re-encode the request body. Without this, the API sees ``meta``
    # as a string and 422s.
    body_data = None
    if meta["body_params"]:
        body_obj = {}
        for p in meta["body_params"]:
            if p not in arguments:
                continue
            v = arguments[p]
            if isinstance(v, str) and v and v[0] in "{[":
                try:
                    v = json.loads(v)
                except json.JSONDecodeError:
                    pass
            body_obj[p] = v
        body_data = json.dumps(body_obj).encode()

    # Make request — identify as MCP proxy so Lab tracks it separately.
    # Include X-Agent-Id when launched in isolated mode so git operations
    # route to this agent's worktree.
    # Include Basic Auth when THE_LAB_USER + THE_LAB_PASSWORD are set so
    # agents work transparently behind the auth gate.
    import base64 as _b64
    headers = {"X-MCP-Proxy": "true"}
    agent_id = os.environ.get("THE_LAB_AGENT_ID")
    if agent_id:
        headers["X-Agent-Id"] = agent_id
    _user = os.environ.get("THE_LAB_USER", "").strip()
    _pw   = os.environ.get("THE_LAB_PASSWORD", "").strip()
    if _user and _pw:
        headers["Authorization"] = "Basic " + _b64.b64encode(
            f"{_user}:{_pw}".encode()
        ).decode()
    if body_data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body_data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return json.dumps({"error": e.code, "reason": e.reason, "detail": body})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── MCP JSON-RPC/stdio protocol ──────────────────────────────────────────────

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    # Fetch OpenAPI spec and build tools on startup
    try:
        spec = fetch_openapi_spec()
        tools, tool_meta = build_tools(spec)
    except Exception as e:
        # If we can't reach the API, start with no tools — we'll retry on first call
        print(f"Warning: could not fetch OpenAPI spec: {e}", file=sys.stderr)
        tools, tool_meta = [], {}

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        msg_id = msg.get("id")

        # Notifications (no id) — acknowledge silently
        if msg_id is None:
            continue

        try:
            # Lazy re-fetch spec if startup fetch failed and tools are still empty
            if not tools and method in ("tools/list", "tools/call"):
                try:
                    spec = fetch_openapi_spec()
                    tools, tool_meta = build_tools(spec)
                    print(f"Reconnected: loaded {len(tools)} tools", file=sys.stderr)
                except Exception as e:
                    print(f"Re-fetch failed: {e}", file=sys.stderr)

            if method == "initialize":
                send({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "labapi", "version": "1.0.0"},
                    },
                })
            elif method == "tools/list":
                send({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"tools": tools},
                })
            elif method == "tools/call":
                params = msg.get("params", {})
                name = params.get("name", "")
                args = params.get("arguments", {})
                if name in tool_meta:
                    text = proxy_call(name, args, tool_meta)
                else:
                    text = json.dumps({"error": f"Unknown tool: {name}"})
                send({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": text}]},
                })
            else:
                send({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })
        except BrokenPipeError:
            # Client closed the pipe — exit cleanly
            break
        except Exception as e:
            print(f"Error handling {method}: {e}", file=sys.stderr)
            try:
                send({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32603, "message": f"Internal error: {e}"},
                })
            except Exception:
                pass


if __name__ == "__main__":
    main()
