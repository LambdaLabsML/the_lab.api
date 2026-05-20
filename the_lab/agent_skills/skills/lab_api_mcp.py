#!/usr/bin/env python3
"""OpenAPI-to-MCP bridge: auto-generates MCP tools from the Lab API's /openapi.json.

Zero dependencies. Reads THE_LAB_API_URL from the environment (set by run_eval.py).
On startup, fetches the OpenAPI spec, builds MCP tool definitions, and proxies
tool calls as HTTP requests to the Lab API.
"""
import base64 as _base64
import hashlib as _hashlib
import json
import os
import socket as _socket
import ssl as _ssl
import struct as _struct
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

# ── Raw WebSocket client (stdlib only) ───────────────────────────────────────
#
# Implements just enough of RFC 6455 for the agent to subscribe to the
# server-push event stream at /api/v1/ws.  Read-only — we only parse
# server→client frames (which are never masked) and respond to pings.

def _ws_connect(host: str, port: int, path_qs: str, use_ssl: bool):
    """Open a raw TCP (or TLS) socket and complete the WebSocket handshake.

    Returns the connected socket with the upgrade confirmed.
    Raises RuntimeError if the server rejects the upgrade.
    """
    key_bytes = _base64.b64encode(os.urandom(16))
    key = key_bytes.decode()
    accept_expected = _base64.b64encode(
        _hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()
    ).decode()

    sock = _socket.create_connection((host, port), timeout=10)
    if use_ssl:
        ctx = _ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    sock.settimeout(None)  # switch to blocking (no timeout after connect)

    req = (
        f"GET {path_qs} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())

    # Read response headers
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("Server closed connection during WS handshake")
        buf += chunk

    head = buf.split(b"\r\n\r\n", 1)[0].decode(errors="replace")
    if "101 Switching Protocols" not in head:
        raise RuntimeError(f"WS upgrade rejected: {head[:120]}")
    if accept_expected not in head:
        raise RuntimeError("WS handshake: Sec-WebSocket-Accept mismatch")
    return sock


def _ws_recv_frame(sock):
    """Read one complete WebSocket frame from *sock*.

    Returns ``(opcode, payload_bytes)``.  Handles 16-bit and 64-bit extended
    payload lengths.  Server→client frames are never masked.
    """
    def _read_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionResetError("WebSocket connection closed by server")
            buf += chunk
        return buf

    header = _read_exact(2)
    # fin    = (header[0] & 0x80) != 0  # we handle fragmentation below
    opcode = header[0] & 0x0F
    # masked = (header[1] & 0x80) != 0  # server→client: always 0
    length = header[1] & 0x7F
    if length == 126:
        length = _struct.unpack("!H", _read_exact(2))[0]
    elif length == 127:
        length = _struct.unpack("!Q", _read_exact(8))[0]
    payload = _read_exact(length)
    return opcode, payload


def _ws_send_pong(sock, payload=b""):
    """Send a masked pong frame (client→server frames must be masked)."""
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    frame = bytes([0x8A, 0x80 | len(payload)]) + mask + masked
    sock.sendall(frame)


def _ws_url_parts():
    """Parse API_BASE and return (host, port, ws_path_prefix, use_ssl)."""
    base = API_BASE
    for suffix in ("/api/v1", "/api/v1/"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    use_ssl = base.startswith("https://")
    bare = base.removeprefix("https://").removeprefix("http://")
    if ":" in bare:
        host, port_str = bare.rsplit(":", 1)
        port = int(port_str)
    else:
        host = bare
        port = 443 if use_ssl else 80
    return host, port, use_ssl


def _ws_path_qs(since: int = 0) -> str:
    token = ""
    user = os.environ.get("THE_LAB_USER", "").strip()
    pw   = os.environ.get("THE_LAB_PASSWORD", "").strip()
    if user and pw:
        token = _base64.b64encode(f"{user}:{pw}".encode()).decode()
    qs = f"?since={since}"
    if token:
        qs += f"&token={token}"
    return f"/api/v1/ws{qs}"


def _watch(
    types: list[str] | None = None,
    keyword: str | None = None,
    timeout: float = 3600,
) -> dict:
    """Block until a matching event arrives on the WebSocket stream.

    Matching criteria (both optional, ANDed if both supplied):
      types   — event ``type`` field must be in this list
      keyword — any string value in the event dict must contain this substring

    Returns the first matching event dict, or ``{"timeout": true}`` if
    *timeout* seconds elapse with no match.
    """
    import time
    host, port, use_ssl = _ws_url_parts()
    deadline = time.monotonic() + timeout

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"timeout": True}

        try:
            sock = _ws_connect(host, port, _ws_path_qs(), use_ssl)
        except Exception as e:
            # Brief pause before retry so we don't spin-loop on a down server.
            time.sleep(min(5.0, remaining))
            continue

        sock.settimeout(1.0)  # short timeout so we can check the deadline
        try:
            fragmented: list[bytes] = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"timeout": True}

                try:
                    opcode, payload = _ws_recv_frame(sock)
                except TimeoutError:
                    continue
                except (ConnectionResetError, OSError):
                    break  # reconnect

                if opcode == 0x9:   # ping
                    _ws_send_pong(sock, payload)
                    continue
                if opcode == 0x8:   # close
                    return {"timeout": True, "reason": "server closed connection"}
                if opcode in (0x1, 0x2):  # text or binary frame (start)
                    fragmented = [payload]
                elif opcode == 0x0:       # continuation frame
                    fragmented.append(payload)
                else:
                    continue

                # FIN bit in original header: we need to re-check it.
                # Simplified: treat every non-continuation frame as complete
                # (the server sends small JSON events, rarely fragmented).
                try:
                    event = json.loads(b"".join(fragmented).decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                # Apply filters
                if types and event.get("type") not in types:
                    continue
                if keyword:
                    flat = json.dumps(event)
                    if keyword.lower() not in flat.lower():
                        continue

                return event
        finally:
            try:
                sock.close()
            except OSError:
                pass


# Hardcoded tool definitions for the two WS tools (not in OpenAPI).
_WS_TOOLS = [
    {
        "name": "watch_events",
        "description": (
            "Block until a specific experiment or system event arrives on the "
            "server's WebSocket stream. Use instead of polling /wait or "
            "repeatedly calling other endpoints.\n\n"
            "Returns the first matching event or {\"timeout\": true} after "
            "``timeout`` seconds.\n\n"
            "Common ``types``:\n"
            "  experiment_finished  — an experiment completed or failed\n"
            "  experiment_started   — scheduler dispatched an experiment\n"
            "  experiment_queued    — a new experiment entered the queue\n"
            "  message_received     — a directed inter-agent message arrived\n"
            "  idea_changed         — an idea was created/concluded/abandoned\n"
            "  queue_changed        — queue state changed (pause/resume etc.)\n"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Event types to match (e.g. ['experiment_finished']). "
                                   "Omit to match any event type.",
                },
                "timeout": {
                    "type": "number",
                    "default": 3600,
                    "description": "Maximum seconds to wait (default 3600).",
                },
            },
        },
    },
    {
        "name": "watch_keyword",
        "description": (
            "Block until an event whose JSON representation contains ``keyword`` "
            "arrives on the WebSocket stream. Optionally also filter by event "
            "type. Useful for watching for a specific experiment label, idea id, "
            "role name, or any other string that would appear in the event payload."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["keyword"],
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Substring to search for in the event JSON "
                                   "(case-insensitive). E.g. '151.3', 'engineer', 'idea/42'.",
                },
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional event type filter (AND-ed with keyword match).",
                },
                "timeout": {
                    "type": "number",
                    "default": 3600,
                    "description": "Maximum seconds to wait (default 3600).",
                },
            },
        },
    },
]


def _call_ws_tool(name: str, args: dict) -> str:
    """Dispatch a WS tool call and return the JSON result string."""
    types   = args.get("types") or None
    keyword = args.get("keyword") or None
    timeout = float(args.get("timeout") or 3600)
    result  = _watch(types=types, keyword=keyword, timeout=timeout)
    return json.dumps(result)


# ── MCP JSON-RPC/stdio protocol ───────────────────────────────────────────────

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
                    "result": {"tools": tools + _WS_TOOLS},
                })
            elif method == "tools/call":
                params = msg.get("params", {})
                name = params.get("name", "")
                args = params.get("arguments", {})
                if name in ("watch_events", "watch_keyword"):
                    text = _call_ws_tool(name, args)
                elif name in tool_meta:
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
