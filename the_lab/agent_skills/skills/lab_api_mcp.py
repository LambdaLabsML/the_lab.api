#!/usr/bin/env python3
"""OpenAPI-to-MCP bridge: auto-generates MCP tools from the Lab API's /openapi.json.

Zero dependencies. Reads THE_LAB_API_URL from the environment (set by run_eval.py).
On startup, fetches the OpenAPI spec, builds MCP tool definitions, and proxies
tool calls as HTTP requests to the Lab API.

Hot reload: a background daemon thread (see "Hot reload" section below)
periodically re-fetches /openapi.json and rebuilds the tool set when it
changes, notifying the client via notifications/tools/list_changed — and
re-execs this process in place (same stdin/stdout) if its own source file on
disk changes, so a relaunched Lab API or an updated bridge no longer requires
agents to manually reconnect the MCP server.
"""
import base64 as _base64
import hashlib as _hashlib
import json
import os
import socket as _socket
import ssl as _ssl
import struct as _struct
import sys
import threading
import urllib.request
import urllib.parse

# ── Configuration ─────────────────────────────────────────────────────────────

API_BASE = os.environ.get("THE_LAB_API_URL", "http://localhost:8000/api/v1")

# Self-signed localhost https can't be verified. The agent launcher sets
# THE_LAB_API_INSECURE=1 when it points us at such an endpoint; in that case we
# disable TLS verification (HTTP and remote valid-cert https are unaffected:
# _SSL_CTX stays None → default behaviour).
def _build_ssl_ctx():
    if os.environ.get("THE_LAB_API_INSECURE", "").strip() not in ("1", "true", "yes"):
        return None
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    return ctx

_SSL_CTX = _build_ssl_ctx()

# Resilience knobs (feedback: API restarts mid-session drop the bridge).
# Retry connection-level failures a few times with linear backoff before
# surfacing an error, so a brief server bounce doesn't fail a tool call.
_MAX_RETRIES = 4
_RETRY_BACKOFF = 0.75  # seconds; multiplied by attempt number

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
    # Feedback: "reopening ideas is not supported" — the server has this route,
    # it just wasn't in the allowlist. Expose it (body {reason} comes from spec).
    ("post", "/api/v1/ideas/{idea_id}/reopen"):    "reopen_idea",
    ("post", "/api/v1/ideas/{idea_id}/note"):      "add_note",
    ("get",  "/api/v1/ideas/{idea_id}/notes"):     "list_notes",
    ("post", "/api/v1/ideas/{idea_id}/experiments"): "create_experiment",
    ("post", "/api/v1/ideas/{idea_id}/experiments/batch"): "create_experiments",
    ("post", "/api/v1/experiments/clone"): "clone_experiment",
    ("get",  "/api/v1/experiments"):               "list_experiments",
    ("get",  "/api/v1/experiments/log"):           "get_failed_logs",
    ("get",  "/api/v1/experiments/tags"):          "list_tags",
    ("post", "/api/v1/experiments/tags/rename"):   "rename_tag",
    ("patch", "/api/v1/experiments/{exp_ref}/tags"): "update_experiment_tags",
    ("post", "/api/v1/experiments/tags/batch"):    "batch_update_tags",
    ("get",  "/api/v1/experiments/{exp_ref}"):     "get_experiment",
    ("get",  "/api/v1/experiments/{exp_ref}/meta"): "get_meta",
    ("get",  "/api/v1/experiments/{exp_ref}/log"): "get_experiment_log",
    ("get",  "/api/v1/experiments/{exp_ref}/progress"): "get_progress",
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
    ("get",  "/api/v1/messages/search"):           "search_messages",
    # Threads: shared, titled conversations agents can find, read and join
    ("get",  "/api/v1/threads"):                   "list_threads",
    ("post", "/api/v1/threads"):                   "create_thread",
    ("get",  "/api/v1/threads/{thread_id}"):       "get_thread",
    ("post", "/api/v1/threads/{thread_id}/join"):  "join_thread",
    ("post", "/api/v1/threads/{thread_id}/leave"): "leave_thread",
    ("post", "/api/v1/threads/{thread_id}/members"): "add_thread_members",
    ("patch", "/api/v1/threads/{thread_id}"):      "update_thread",
}

# ── OpenAPI parsing ───────────────────────────────────────────────────────────

def fetch_openapi_spec():
    """Fetch /openapi.json from the Lab API (with connection-error retries)."""
    url = f"{_api_root()}/openapi.json"
    headers = {}
    _user = os.environ.get("THE_LAB_USER", "").strip()
    _pw   = os.environ.get("THE_LAB_PASSWORD", "").strip()
    if _user and _pw:
        headers["Authorization"] = "Basic " + _base64.b64encode(
            f"{_user}:{_pw}".encode()
        ).decode()
    req = urllib.request.Request(url, headers=headers)
    # Retry transient connection failures (server restarting) with backoff.
    import time
    last_err = None
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                if attempt > 0:
                    _wake_refresh.set()  # recovered after a transient failure — recheck soon
                return json.loads(resp.read())
        except (urllib.error.URLError, ConnectionError, _socket.timeout, OSError) as e:
            last_err = e
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
    raise last_err


def _api_root():
    """API base with the /api/v1 suffix stripped (shared by several helpers)."""
    base = API_BASE
    for suffix in ("/api/v1", "/api/v1/"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def probe_health():
    """Lightweight liveness probe: is the server up enough to rebuild tools?

    Prefers ``GET /api/v1/health`` (being added by another fixer) but tolerates
    its absence on older servers — any HTTP *response* (even 404) proves the
    server is answering, so we treat that as "alive" and let the caller proceed
    to fetch the spec. Only a connection-level failure counts as "down".
    """
    url = f"{_api_root()}/api/v1/health"
    headers = {}
    _user = os.environ.get("THE_LAB_USER", "").strip()
    _pw   = os.environ.get("THE_LAB_PASSWORD", "").strip()
    if _user and _pw:
        headers["Authorization"] = "Basic " + _base64.b64encode(
            f"{_user}:{_pw}".encode()
        ).decode()
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5, context=_SSL_CTX):
            return True
    except urllib.error.HTTPError:
        return True  # server answered (e.g. 404 on older servers) → alive
    except Exception:
        return False  # connection refused/reset/empty reply → down


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

    Pydantic puts ``description``/``title``/``default`` on the OUTER anyOf
    wrapper, not inside the branch itself — every ``Field(description=...)``
    on an ``Optional`` field lived there and was silently dropped by this
    unwrap (the MCP tool showed the field with no description at all).
    Preserve them onto the flattened branch.
    """
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            if branch.get("type") == "null":
                continue
            if "$ref" in branch:
                branch = resolve_ref(branch["$ref"], spec)
            merged = dict(branch)
            for k in ("description", "title", "default"):
                if k in schema and k not in merged:
                    merged[k] = schema[k]
            return merged
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


# ── Hot reload ────────────────────────────────────────────────────────────────
#
# One background daemon thread (started from main(), never at import time —
# tests that just exec this module to call a helper shouldn't get a stray
# thread making network calls) drives two independent mechanisms:
#
#   1. Tool-list refresh: periodically re-fetch /openapi.json; if its hash
#      changed, rebuild tools/tool_meta and tell the client via
#      notifications/tools/list_changed.
#   2. Self-reload: if this file's own bytes changed on disk, re-exec the
#      process in place (os.execv keeps stdin/stdout attached, so the MCP
#      connection survives) once the main loop is idle between messages.
#
# tools/tool_meta are published together as one _ToolState instance so a
# concurrent tools/call always sees a matched pair — rebinding the
# module-level `_state` name is a single atomic operation (no reader lock
# needed; `_state_lock` only serializes the writers against each other).

_REFRESH_INTERVAL_S = float(os.environ.get("LAB_MCP_REFRESH_S", "30") or 30)
_SOURCE_PATH = os.path.abspath(__file__)

_stdout_lock = threading.Lock()       # every stdout write (response or notification)
_busy_lock = threading.Lock()         # held by the main loop only while handling one message
_state_lock = threading.Lock()        # serializes writers of `_state`
_reload_pending = threading.Event()   # set when self-reload couldn't run immediately
_wake_refresh = threading.Event()     # poke the background thread to check sooner
_initialized_event = threading.Event()  # has the client's `initialize` been handled?


class _ToolState:
    """Immutable snapshot of the built tool set — swapped, never mutated."""
    __slots__ = ("tools", "tool_meta", "spec_hash")

    def __init__(self, tools, tool_meta, spec_hash):
        self.tools = tools
        self.tool_meta = tool_meta
        self.spec_hash = spec_hash


_state = _ToolState([], {}, None)


def _hash_spec(spec) -> str:
    return _hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def _publish_tools(new_tools, new_meta, spec_hash) -> bool:
    """Swap in a freshly built tool set.

    Returns True if it differs from the previously published one, so the
    caller can decide whether a list_changed notification is warranted.
    """
    global _state
    with _state_lock:
        changed = spec_hash != _state.spec_hash
        _state = _ToolState(new_tools, new_meta, spec_hash)
    return changed


def _notify_tools_changed():
    # Only after the client has completed `initialize` — the MCP handshake
    # requires that, and some clients would reject/ignore an earlier one.
    if _initialized_event.is_set():
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})


def _restore_watch_cursors():
    """Re-seed implicit watch cursors carried across a self-reload exec.

    Cheap to pass through (just a small seq-number map), so we do; if this
    env var is absent or unparseable, matching watches simply resume from
    "now" on their next call, same as a cold start.
    """
    raw = os.environ.get("LAB_MCP_WATCH_CURSORS", "")
    if not raw:
        return
    try:
        for key_json, seq in json.loads(raw).items():
            types_list, keyword = json.loads(key_json)
            _IMPLICIT_WATCH_CURSORS[(tuple(types_list), keyword)] = seq
    except Exception:
        pass  # non-fatal: worst case, watches restart from "now"


def _exec_reload():
    """Re-exec this process in place, keeping stdin/stdout attached.

    Must only be called while the main loop is not mid-message (see
    `_do_self_reload`) — i.e. it is either blocked reading the next stdin
    line, or about to be.
    """
    try:
        os.environ["LAB_MCP_RELOADED"] = "1"
        if _initialized_event.is_set():
            os.environ["LAB_MCP_INITIALIZED"] = "1"
        if _IMPLICIT_WATCH_CURSORS:
            os.environ["LAB_MCP_WATCH_CURSORS"] = json.dumps({
                json.dumps([list(types_key), keyword]): seq
                for (types_key, keyword), seq in _IMPLICIT_WATCH_CURSORS.items()
            })
        print("[hot-reload] source changed, re-executing", file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)
        # os.execv does not return on success.
    except Exception as e:
        # A reload attempt must never take the bridge down.
        print(f"[hot-reload] self-exec failed, continuing on old code: {e}", file=sys.stderr)


def _do_self_reload():
    """Ask the main loop to re-exec after its next response.

    Only the main loop execs, and only with no unread input buffered: a
    request already read from stdin but not yet answered would otherwise be
    lost with the old process, leaving the client's tool call hanging. While
    the client is idle the reload therefore waits for its next request (new
    tools still arrive at once via the refresh notification)."""
    _reload_pending.set()


# stdin is read through our own buffer (not ``for line in sys.stdin``, whose
# read-ahead is invisible) so a re-exec can tell that no request is pending.
_stdin_buf = bytearray()


def _stdin_lines():
    fd = sys.stdin.fileno()
    while True:
        nl = _stdin_buf.find(b"\n")
        if nl >= 0:
            line = bytes(_stdin_buf[:nl])
            del _stdin_buf[: nl + 1]
            yield line.decode("utf-8", "replace")
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            if _stdin_buf:
                line = bytes(_stdin_buf)
                _stdin_buf.clear()
                yield line.decode("utf-8", "replace")
            return
        _stdin_buf.extend(chunk)


def _check_self_reload():
    try:
        with open(_SOURCE_PATH, "rb") as f:
            current = _hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return
    if current != _SOURCE_HASH_AT_START:
        _do_self_reload()


def _check_tool_refresh():
    if not probe_health():
        return  # server down — stay quiet, retried on the next tick
    try:
        spec = fetch_openapi_spec()
    except Exception as e:
        print(f"[hot-reload] refresh fetch failed: {e}", file=sys.stderr)
        return
    new_tools, new_meta = build_tools(spec)
    if _publish_tools(new_tools, new_meta, _hash_spec(spec)):
        print(f"[hot-reload] tool set changed ({len(new_tools)} tools)", file=sys.stderr)
        _notify_tools_changed()


def _refresh_loop():
    """Background daemon thread: periodic tool refresh + self-reload check."""
    while True:
        _wake_refresh.wait(timeout=_REFRESH_INTERVAL_S)
        _wake_refresh.clear()
        try:
            _check_self_reload()
            _check_tool_refresh()
        except Exception as e:
            # Must never crash the bridge — log and keep going.
            print(f"[hot-reload] background check failed: {e}", file=sys.stderr)


_SOURCE_HASH_AT_START = None
try:
    with open(_SOURCE_PATH, "rb") as _f:
        _SOURCE_HASH_AT_START = _hashlib.sha256(_f.read()).hexdigest()
except OSError:
    pass


# ── HTTP proxy ────────────────────────────────────────────────────────────────

def proxy_call(tool_name, arguments, tool_meta):
    """Execute an HTTP request to the Lab API and return the response text."""
    meta = tool_meta[tool_name]
    method = meta["method"]
    path = meta["path_template"]

    # Substitute path parameters.
    # Feedback: cancel/rerun returned "405 Method Not Allowed". Root cause: a
    # missing/empty path param (e.g. exp_ref) left a placeholder that resolved
    # to an empty segment, so ``/experiments/{exp_ref}/cancel`` became
    # ``/experiments//cancel`` → Starlette normalises the double slash to
    # ``/experiments/cancel``, which matches the GET ``/experiments/{exp_ref}``
    # route (exp_ref="cancel") and rejects POST with 405 (Allow: GET) — a
    # misleading error. Fail fast with a clear message instead.
    for p in meta["path_params"]:
        val = arguments.get(p)
        if val is None or str(val).strip() == "":
            return json.dumps({
                "error": "missing_path_parameter",
                "detail": f"'{p}' is required and must be non-empty for tool "
                          f"'{tool_name}'.",
            })
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

    # Feedback: "API restarts mid-session ... return empty replies (curl exit
    # 52) — tools vanish." A transient connection error (empty reply / refused
    # / reset) usually means the server is restarting; retry a few times with a
    # short backoff before giving up. HTTP errors (4xx/5xx) are *responses* —
    # they're returned as-is, not retried.
    import time
    last_err = None
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=300, context=_SSL_CTX) as resp:
                if attempt > 0:
                    _wake_refresh.set()  # recovered after a transient failure — recheck soon
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            return json.dumps({"error": e.code, "reason": e.reason, "detail": body})
        except (urllib.error.URLError, ConnectionError, _socket.timeout, OSError) as e:
            # Connection-level failure — server likely bouncing. Back off + retry.
            last_err = e
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
        except Exception as e:
            return json.dumps({"error": str(e)})
    return json.dumps({"error": str(last_err), "detail": "connection failed after retries"})


# ── MCP JSON-RPC/stdio protocol ──────────────────────────────────────────────

# ── Raw WebSocket client (stdlib only) ───────────────────────────────────────
#
# Implements just enough of RFC 6455 for the agent to subscribe to the
# server-push event stream at /api/v1/ws.  Read-only — we only parse
# server→client frames (which are never masked) and respond to pings.

class _BufferedSocket:
    """Socket proxy which yields bytes received alongside the WS handshake."""
    def __init__(self, sock, buffered: bytes = b""):
        self._sock = sock
        self._buffered = buffered

    def recv(self, size: int):
        if self._buffered:
            chunk, self._buffered = self._buffered[:size], self._buffered[size:]
            return chunk
        return self._sock.recv(size)

    def __getattr__(self, name):
        return getattr(self._sock, name)


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
        ctx = _SSL_CTX or _ssl.create_default_context()
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

    head_bytes, leftover = buf.split(b"\r\n\r\n", 1)
    head = head_bytes.decode(errors="replace")
    if "101 Switching Protocols" not in head:
        raise RuntimeError(f"WS upgrade rejected: {head[:120]}")
    if accept_expected not in head:
        raise RuntimeError("WS handshake: Sec-WebSocket-Accept mismatch")
    # A fast server may coalesce the first event frame with its HTTP 101
    # response. Keep those bytes rather than silently dropping that event.
    return _BufferedSocket(sock, leftover)


def _ws_recv_frame(sock):
    """Read one complete WebSocket frame from *sock*.

    Returns ``(opcode, payload_bytes)``.  Handles 16-bit and 64-bit extended
    payload lengths.  Server→client frames are never masked.
    """
    # Preserve partially received frames across socket timeouts.  The watch
    # loop deliberately uses a short timeout to enforce its deadline, so a
    # slow frame must not be mistaken for a new frame on the next iteration.
    buf = getattr(sock, "_mcp_ws_frame_buffer", b"")

    def _fill(n):
        nonlocal buf
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except Exception:
                sock._mcp_ws_frame_buffer = buf
                raise
            if not chunk:
                sock._mcp_ws_frame_buffer = buf
                raise ConnectionResetError("WebSocket connection closed by server")
            buf += chunk

    _fill(2)
    header = buf[:2]
    # fin    = (header[0] & 0x80) != 0  # we handle fragmentation below
    opcode = header[0] & 0x0F
    # masked = (header[1] & 0x80) != 0  # server→client: always 0
    length = header[1] & 0x7F
    header_len = 2
    if length == 126:
        _fill(4)
        length = _struct.unpack("!H", buf[2:4])[0]
        header_len = 4
    elif length == 127:
        _fill(10)
        length = _struct.unpack("!Q", buf[2:10])[0]
        header_len = 10
    # Server frames are not masked, but accepting a masked frame here costs
    # little and keeps the parser correctly aligned with unusual test/proxy
    # implementations.
    masked = (header[1] & 0x80) != 0
    if masked:
        _fill(header_len + 4)
        mask = buf[header_len:header_len + 4]
        header_len += 4
    _fill(header_len + length)
    payload = buf[header_len:header_len + length]
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock._mcp_ws_frame_buffer = buf[header_len + length:]
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


def _event_headers() -> dict:
    """Headers shared by the events endpoint and its MCP HTTP fallback."""
    headers = {"X-MCP-Proxy": "true"}
    agent_id = os.environ.get("THE_LAB_AGENT_ID", "").strip()
    if agent_id:
        headers["X-Agent-Id"] = agent_id
    user = os.environ.get("THE_LAB_USER", "").strip()
    pw = os.environ.get("THE_LAB_PASSWORD", "").strip()
    if user and pw:
        headers["Authorization"] = "Basic " + _base64.b64encode(
            f"{user}:{pw}".encode()
        ).decode()
    return headers


def _event_seq(event: dict, after_seq: int) -> int | None:
    """Return a newly consumed event's sequence number, if it is newer."""
    try:
        seq = int(event.get("seq"))
    except (TypeError, ValueError):
        return None
    return seq if seq > after_seq else None


_AGENT_ROLE: str | None = None
_AGENT_ROLE_LOADED = False


def _current_agent_role() -> str | None:
    """Best-effort role lookup for directed message event filtering."""
    global _AGENT_ROLE, _AGENT_ROLE_LOADED
    if _AGENT_ROLE_LOADED:
        return _AGENT_ROLE
    _AGENT_ROLE_LOADED = True
    agent_id = os.environ.get("THE_LAB_AGENT_ID", "").strip()
    if not agent_id:
        return None
    req = urllib.request.Request(f"{_api_root()}/api/v1/agents/{agent_id}",
                                 headers=_event_headers())
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            _AGENT_ROLE = (json.loads(resp.read()).get("role") or None)
    except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError,
            _socket.timeout, OSError, ValueError):
        pass
    return _AGENT_ROLE


def _message_is_for_current_agent(event: dict) -> bool:
    """Apply inbox semantics to generic-stream ``message_received`` events."""
    agent_id = os.environ.get("THE_LAB_AGENT_ID", "").strip()
    if not agent_id:  # operator/generic consumers intentionally see the stream
        return True
    if event.get("from_agent") == agent_id:
        return False  # sending a message must not wake our inbox watcher
    target = event.get("to")
    if target == "all" or target == f"agent:{agent_id}":
        return True
    if isinstance(target, str) and target.startswith("role:"):
        role = _current_agent_role()
        # If registry lookup itself is unavailable, fail open: it is safer not
        # to drop a legitimate role-addressed message from a generic watcher.
        return role is None or target == f"role:{role}"
    return False


def _event_matches(event: dict, types: list[str] | None, keyword: str | None) -> bool:
    if types and event.get("type") not in types:
        return False
    if event.get("type") == "message_received" and not _message_is_for_current_agent(event):
        return False
    return not keyword or keyword.lower() in json.dumps(event).lower()


def _current_event_cursor(deadline: float) -> int | None:
    """Read the stream's current cursor without exposing its old events.

    A new implicit watch starts at this cursor, rather than replaying the
    journal from zero.  The endpoint's envelope reports the newest sequence;
    its events are deliberately discarded here.  ``None`` means that a safe
    baseline could not be obtained.
    """
    import time
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0
    url = f"{_api_root()}/api/v1/events?" + urllib.parse.urlencode({
        "tail": "true", "timeout": 0,
    })
    req = urllib.request.Request(url, headers=_event_headers())
    try:
        with urllib.request.urlopen(req, timeout=min(30, max(1, remaining)),
                                    context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
        seq = data.get("seq", 0)
        return max(0, int(seq))
    except (urllib.error.URLError, ConnectionError, _socket.timeout, OSError,
            ValueError, TypeError):
        return None


def _events_longpoll(
    types: list[str] | None,
    keyword: str | None,
    deadline: float,
    after_seq: int,
) -> dict:
    """WS-less fallback: drain GET /api/v1/events (same stream, same
    envelope) until a matching event arrives or the deadline passes. Used
    when the WebSocket handshake keeps failing (proxy strips upgrades,
    older server, restrictive network) — behaviour matches _watch()."""
    import time
    headers = _event_headers()
    since = after_seq
    first = True
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"timeout": True, "after_seq": since}
        wait = 0 if first else min(25, max(1, int(remaining)))
        first = False
        qs = {"since": since, "timeout": wait}
        url = f"{_api_root()}/api/v1/events?" + urllib.parse.urlencode(qs)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=wait + 30,
                                        context=_SSL_CTX) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, ConnectionError,
                _socket.timeout, OSError, ValueError):
            time.sleep(min(5.0, max(0.5, remaining)))
            continue
        for event in data.get("events") or []:
            seq = _event_seq(event, since)
            if seq is None:
                continue
            # Advance one event at a time.  In particular, do not adopt the
            # response envelope's final seq before returning the first match:
            # another matching event may be later in this same response.
            since = seq
            if _event_matches(event, types, keyword):
                return event


def _watch(
    types: list[str] | None = None,
    keyword: str | None = None,
    timeout: float = 3600,
    after_seq: int = 0,
) -> dict:
    """Block until a matching event arrives on the WebSocket stream.

    Matching criteria (both optional, ANDed if both supplied):
      types   — event ``type`` field must be in this list
      keyword — any string value in the event dict must contain this substring

    Returns the first matching event dict, or ``{"timeout": true}`` if
    *timeout* seconds elapse with no match. If the WebSocket handshake fails
    repeatedly, falls back to long-polling GET /api/v1/events — same stream,
    same envelope. ``after_seq`` is the exclusive resume cursor.
    """
    import time
    host, port, use_ssl = _ws_url_parts()
    deadline = time.monotonic() + timeout
    ws_failures = 0
    since = after_seq

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"timeout": True, "after_seq": since}

        try:
            sock = _ws_connect(host, port, _ws_path_qs(since), use_ssl)
            ws_failures = 0
        except Exception as e:
            ws_failures += 1
            if ws_failures >= 2:
                # WS path is not viable here — switch to the HTTP drain.
                return _events_longpoll(types, keyword, deadline, since)
            # Brief pause before retry so we don't spin-loop on a down server.
            time.sleep(min(5.0, remaining))
            continue

        sock.settimeout(1.0)  # short timeout so we can check the deadline
        try:
            fragmented: list[bytes] = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"timeout": True, "after_seq": since}

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
                    return {"timeout": True, "reason": "server closed connection",
                            "after_seq": since}
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

                seq = _event_seq(event, since)
                if seq is None:
                    continue
                since = seq
                if not _event_matches(event, types, keyword):
                    continue

                return event
        finally:
            try:
                sock.close()
            except OSError:
                pass


# Hardcoded tool definitions for the two WS tools (not in OpenAPI).
_IMPLICIT_WATCH_CURSORS: dict[tuple[tuple[str, ...], str], int] = {}


def _watch_cursor_key(types: list[str] | None, keyword: str | None) -> tuple[tuple[str, ...], str]:
    """Canonical key for a watch's matching subscription."""
    return tuple(sorted(set(types or []))), (keyword or "").casefold()


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
                "after_seq": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Exclusive event cursor to resume from. Specify 0 to "
                                   "replay available history. When omitted, this matching "
                                   "watch resumes from its prior call (or starts at the "
                                   "current stream tail).",
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
                "after_seq": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Exclusive event cursor to resume from. Specify 0 to "
                                   "replay available history. When omitted, this matching "
                                   "watch resumes from its prior call (or starts at the "
                                   "current stream tail).",
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
    explicit_cursor = "after_seq" in args and args.get("after_seq") is not None
    if explicit_cursor:
        try:
            after_seq = max(0, int(args["after_seq"]))
        except (TypeError, ValueError):
            return json.dumps({"error": "after_seq must be a non-negative integer"})
        cursor_key = None
    else:
        cursor_key = _watch_cursor_key(types, keyword)
        after_seq = _IMPLICIT_WATCH_CURSORS.get(cursor_key)
        if after_seq is None:
            deadline = __import__("time").monotonic() + timeout
            after_seq = _current_event_cursor(deadline)
            if after_seq is None:
                return json.dumps({
                    "error": "could not establish event cursor; retry or provide after_seq",
                })

    result = _watch(types=types, keyword=keyword, timeout=timeout,
                    after_seq=after_seq)
    if cursor_key is not None:
        # A returned event's seq, or a timeout's after_seq, is exactly the
        # consumed prefix. Never store an envelope's later cursor here.
        next_seq = result.get("after_seq", result.get("seq"))
        try:
            if next_seq is not None:
                _IMPLICIT_WATCH_CURSORS[cursor_key] = max(after_seq, int(next_seq))
        except (TypeError, ValueError):
            pass
    return json.dumps(result)


# ── MCP JSON-RPC/stdio protocol ───────────────────────────────────────────────

def send(msg):
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def main():
    _restore_watch_cursors()
    if os.environ.get("LAB_MCP_INITIALIZED") == "1":
        # We're the result of a self-reload exec — the client already did
        # the MCP handshake against the old process and won't send it again.
        _initialized_event.set()
    reloaded = os.environ.get("LAB_MCP_RELOADED") == "1"

    # Fetch OpenAPI spec and build tools on startup
    try:
        spec = fetch_openapi_spec()
        new_tools, new_meta = build_tools(spec)
        _publish_tools(new_tools, new_meta, _hash_spec(spec))
    except Exception as e:
        # If we can't reach the API, start with no tools — we'll retry on first call
        print(f"Warning: could not fetch OpenAPI spec: {e}", file=sys.stderr)

    if reloaded:
        # Tell the (already-initialized) client its tool list may have moved
        # — it won't otherwise know this is a fresh process.
        _notify_tools_changed()

    threading.Thread(target=_refresh_loop, daemon=True, name="lab-mcp-refresh").start()

    for raw in _stdin_lines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        # Handling one message; the background refresh thread serializes its
        # tool-state swaps against this via _state_lock, and self-reload waits
        # for the main loop (see `_do_self_reload`).
        with _busy_lock:
            method = msg.get("method")
            msg_id = msg.get("id")

            # Notifications (no id) — acknowledge silently
            if msg_id is None:
                continue

            try:
                # Lazy (re-)build of tools. Feedback: an API restart mid-session
                # drops the bridge and "tools vanish until something pokes
                # ToolSearch". If the startup fetch failed (server down at boot) or
                # the tool set is empty, rebuild on the next tools/list or
                # tools/call. We use a quick health/liveness probe first (handling a
                # missing /health gracefully) so we only attempt a rebuild when the
                # server is actually answering again.
                if not _state.tools and method in ("tools/list", "tools/call"):
                    if probe_health():
                        try:
                            spec = fetch_openapi_spec()
                            new_tools, new_meta = build_tools(spec)
                            _publish_tools(new_tools, new_meta, _hash_spec(spec))
                            print(f"Reconnected: loaded {len(new_tools)} tools", file=sys.stderr)
                        except Exception as e:
                            print(f"Re-fetch failed: {e}", file=sys.stderr)
                    else:
                        print("Health probe: server still unavailable, "
                              "keeping tools empty (will retry next call)",
                              file=sys.stderr)

                if method == "initialize":
                    _initialized_event.set()
                    send({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {"listChanged": True}},
                            "serverInfo": {"name": "labapi", "version": "1.0.0"},
                        },
                    })
                elif method == "tools/list":
                    send({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"tools": _state.tools + _WS_TOOLS},
                    })
                elif method == "tools/call":
                    params = msg.get("params", {})
                    name = params.get("name", "")
                    args = params.get("arguments", {})
                    if name in ("watch_events", "watch_keyword"):
                        text = _call_ws_tool(name, args)
                    elif name in _state.tool_meta:
                        text = proxy_call(name, args, _state.tool_meta)
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

        # Between messages: if the bridge's code changed, re-exec now — but
        # only with nothing unread buffered (else after the next message).
        if _reload_pending.is_set() and not _stdin_buf:
            _reload_pending.clear()
            _exec_reload()


if __name__ == "__main__":
    main()
