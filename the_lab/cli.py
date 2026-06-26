"""CLI entrypoint for The Lab API server."""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .sandbox import save_runtime_info

# ---------------------------------------------------------------------------
# Color helpers (ANSI, auto-disabled when stdout is not a TTY)
# ---------------------------------------------------------------------------

def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"

def _green(text: str) -> str: return _color(text, "32")
def _yellow(text: str) -> str: return _color(text, "33")
def _blue(text: str) -> str: return _color(text, "34")
def _bold(text: str) -> str: return _color(text, "1")
def _dim(text: str) -> str: return _color(text, "2")

# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

def _ask_yn(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{_blue('?')} {question} {_dim(suffix)} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer in ("y", "yes")

# ---------------------------------------------------------------------------
# Template for PROMPT.md
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = Path(__file__).parent / "PROMPT_template.md"

# ---------------------------------------------------------------------------
# init subcommand
# ---------------------------------------------------------------------------

def cmd_init(target: str | None = None):
    """Walk users through setting up a new project for The Lab."""
    repo = Path(target or ".").resolve()

    print(f"\n{_bold('The Lab')} -- Project Setup\n")

    # 1. Git check -----------------------------------------------------------
    if not (repo / ".git").exists():
        if _ask_yn(f"  {repo} is not a git repository. Initialize one?"):
            subprocess.run(["git", "init"], cwd=str(repo), check=True)
            print(f"  {_green(chr(10003))} Initialized git repository")
        else:
            print(f"  {_yellow('!')} Skipping -- The Lab requires a git repository")
            return
    else:
        print(f"  {_green(chr(10003))} Git repository found: {repo}")

    # 2. PROMPT.md ---------------------------------------------------
    # New layout: all prompt files live under .the_lab/ (PROMPT.md for the
    # default role, PROMPT.<role>.md for named roles). Offer to migrate
    # from the legacy <repo>/PROMPT.md location on first init.
    lab_dir = repo / ".the_lab"
    canonical_prompt = lab_dir / "PROMPT.md"
    legacy_prompt = repo / "PROMPT.md"

    if canonical_prompt.exists():
        print(f"  {_green(chr(10003))} .the_lab/PROMPT.md already exists")
    elif legacy_prompt.exists():
        if _ask_yn(
            "  Found PROMPT.md at the repo root. Move it into .the_lab/ "
            "(needed for role-based prompts)?",
            default=True,
        ):
            lab_dir.mkdir(parents=True, exist_ok=True)
            legacy_prompt.rename(canonical_prompt)
            print(f"  {_green(chr(10003))} Moved PROMPT.md -> .the_lab/PROMPT.md")
        else:
            print(f"  {_dim('-')} Kept PROMPT.md at the repo root (legacy fallback still works)")
    else:
        lab_dir.mkdir(parents=True, exist_ok=True)
        canonical_prompt.write_text(_PROMPT_TEMPLATE.read_text())
        print(f"  {_green(chr(10003))} Created .the_lab/PROMPT.md -- edit this with your research problem")

    # 3. preamble.sh ---------------------------------------------------------
    # Sourced at the top of every experiment wrapper script. Projects can
    # customise it freely; the default is a no-op that just exports the shared
    # dir so downstream scripts can locate shared artifacts.
    preamble_dst = lab_dir / "preamble.sh"
    if preamble_dst.exists():
        print(f"  {_green(chr(10003))} .the_lab/preamble.sh already exists")
    else:
        lab_dir.mkdir(parents=True, exist_ok=True)
        preamble_dst.write_text(
            "#!/usr/bin/env bash\n"
            "# .the_lab/preamble.sh — sourced at the start of every experiment script.\n"
            "# Add project-wide setup here: activate virtualenvs, set env vars, etc.\n"
            "# This file is gitignored and safe to edit freely.\n"
            "\n"
            '_the_lab_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
            'export THE_LAB_SHARED_DIR="${_the_lab_dir}"\n'
        )
        preamble_dst.chmod(0o755)
        print(f"  {_green(chr(10003))} Created .the_lab/preamble.sh -- add project setup here (activate venv, etc.)")

    # 4. pre-commit hook — blocks staged changes to blocked files ---------------
    hooks_dir = repo / ".git" / "hooks"
    hook_path = hooks_dir / "pre-commit"
    hook_body = (
        "#!/usr/bin/env bash\n"
        "# Installed by the-lab init — prevents committing changes to blocked files.\n"
        "blocked='.the_lab/blocked_files.txt'\n"
        "[ -f \"$blocked\" ] || exit 0\n"
        "while IFS= read -r file || [ -n \"$file\" ]; do\n"
        "  # strip comments and blank lines\n"
        "  file=\"${file%%#*}\"\n"
        "  file=\"${file//[[:space:]]/}\"\n"
        "  [ -z \"$file\" ] && continue\n"
        "  if git diff --cached --name-only | grep -qxF \"$file\"; then\n"
        "    echo \"error: commit blocked — '$file' is in .the_lab/blocked_files.txt\" >&2\n"
        "    exit 1\n"
        "  fi\n"
        "done < \"$blocked\"\n"
    )
    if hooks_dir.exists():
        if hook_path.exists():
            print(f"  {_green(chr(10003))} .git/hooks/pre-commit already exists")
        else:
            hook_path.write_text(hook_body)
            hook_path.chmod(0o755)
            print(f"  {_green(chr(10003))} Installed .git/hooks/pre-commit (blocks commits to blocked files)")
    else:
        print(f"  {_dim('-')} Skipped pre-commit hook (.git/hooks/ not found)")

    # 5. MCP bridge ----------------------------------------------------------
    _pkg_skills = Path(__file__).parent / "agent_skills"
    mcp_script_src = _pkg_skills / "skills" / "lab_api_mcp.py"
    mcp_json_src = _pkg_skills / "mcp.json"

    if mcp_script_src.exists():
        import json as _json
        import shutil as _shutil

        mcp_dst = repo / ".claude" / "skills" / "lab_api_mcp.py"
        mcp_json_dst = repo / ".mcp.json"

        # -- Bridge script (.claude/skills/lab_api_mcp.py) --
        if mcp_dst.exists():
            if _ask_yn("  .claude/skills/lab_api_mcp.py exists. Overwrite with latest?", default=False):
                _shutil.copy2(mcp_script_src, mcp_dst)
                print(f"  {_green(chr(10003))} Updated lab_api_mcp.py")
            else:
                print(f"  {_dim('-')} Kept existing lab_api_mcp.py")
        elif _ask_yn("  Install MCP bridge? (lets agents use typed tool calls instead of curl)"):
            mcp_dst.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(mcp_script_src, mcp_dst)
            print(f"  {_green(chr(10003))} Installed .claude/skills/lab_api_mcp.py")
        else:
            print(f"  {_dim('-')} Skipped MCP bridge")

        # -- Claude settings (.claude/settings.json — permissions + MCP registration) --
        settings_src = _pkg_skills / "settings.json"
        settings_dst = repo / ".claude" / "settings.json"
        if settings_src.exists():
            if settings_dst.exists():
                # Merge: add missing mcpServers and permissions
                try:
                    existing_settings = _json.loads(settings_dst.read_text())
                except (ValueError, OSError):
                    existing_settings = {}
                new_settings = _json.loads(settings_src.read_text())
                merged = False
                # Merge mcpServers
                for k, v in new_settings.get("mcpServers", {}).items():
                    if k not in existing_settings.get("mcpServers", {}):
                        existing_settings.setdefault("mcpServers", {})[k] = v
                        merged = True
                # Merge permissions.allow
                existing_allow = set(existing_settings.get("permissions", {}).get("allow", []))
                for perm in new_settings.get("permissions", {}).get("allow", []):
                    if perm not in existing_allow:
                        existing_settings.setdefault("permissions", {}).setdefault("allow", []).append(perm)
                        merged = True
                if merged:
                    settings_dst.write_text(_json.dumps(existing_settings, indent=2) + "\n")
                    print(f"  {_green(chr(10003))} Merged Lab permissions into .claude/settings.json")
                else:
                    print(f"  {_green(chr(10003))} .claude/settings.json already configured")
            else:
                settings_dst.parent.mkdir(parents=True, exist_ok=True)
                _shutil.copy2(settings_src, settings_dst)
                print(f"  {_green(chr(10003))} Created .claude/settings.json")

        # -- MCP config (.mcp.json) --
        if mcp_json_src.exists():
            new_servers = _json.loads(mcp_json_src.read_text()).get("mcpServers", {})
            if mcp_json_dst.exists():
                try:
                    existing_cfg = _json.loads(mcp_json_dst.read_text())
                except (ValueError, OSError):
                    existing_cfg = {}
                existing_servers = existing_cfg.get("mcpServers", {})
                # Check which of our servers are missing
                missing = {k: v for k, v in new_servers.items() if k not in existing_servers}
                if not missing:
                    print(f"  {_green(chr(10003))} .mcp.json already has labapi server")
                elif _ask_yn(f"  .mcp.json exists. Add {', '.join(missing.keys())} server(s) to it?"):
                    existing_servers.update(missing)
                    existing_cfg["mcpServers"] = existing_servers
                    mcp_json_dst.write_text(_json.dumps(existing_cfg, indent=2) + "\n")
                    print(f"  {_green(chr(10003))} Merged {', '.join(missing.keys())} into .mcp.json")
                else:
                    print(f"  {_dim('-')} Kept existing .mcp.json")
            else:
                _shutil.copy2(mcp_json_src, mcp_json_dst)
                print(f"  {_green(chr(10003))} Created .mcp.json")
    else:
        print(f"  {_dim('-')} MCP bridge not found in package (agent_skills/ missing)")

    # 6. .gitignore ----------------------------------------------------------
    gitignore = repo / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    lines = existing.splitlines()

    entries_to_add = []
    for entry in [".the_lab/", ".claude/", ".mcp.json", "PROMPT.md", ".the_lab.agentid"]:
        if not any(line.strip() == entry or line.strip() == entry.rstrip("/") for line in lines):
            entries_to_add.append(entry)

    if entries_to_add:
        if _ask_yn(f"  Add {', '.join(entries_to_add)} to .gitignore?"):
            with open(gitignore, "a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n# The Lab (generated data)\n")
                for entry in entries_to_add:
                    f.write(entry + "\n")
            print(f"  {_green(chr(10003))} Updated .gitignore")
        else:
            print(f"  {_yellow('!')} Skipped -- remember to gitignore .the_lab/ and .claude/ manually")
    else:
        print(f"  {_green(chr(10003))} .gitignore already includes .the_lab/ and .claude/")

    # 7. Pre-fill PROMPT.md with Claude --------------------------------
    # Pick the active prompt file: prefer .the_lab/PROMPT.md (canonical),
    # fall back to legacy <repo>/PROMPT.md if the user declined migration.
    active_prompt = canonical_prompt if canonical_prompt.exists() else legacy_prompt
    import shutil as _shutil
    claude_bin = _shutil.which("claude")
    if claude_bin and active_prompt.exists():
        print(f"\n  {_blue('?')} Describe your research goal so Claude can pre-fill PROMPT.md.")
        print(f"    {_dim('Leave blank to skip and edit the file yourself.')}")
        try:
            user_goal = input(f"    {_dim('>')} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            user_goal = ""
        if user_goal:
            print(f"  {_dim('...')} Claude is analyzing the repo...\n")
            prefill_prompt = (
                "You are helping set up a research project for The Lab, an experiment "
                "management system. The user described their goal as:\n\n"
                f"  \"{user_goal}\"\n\n"
                "Analyze this repository — look at the README, code, scripts, data "
                "directories, configs — and fill in PROMPT.md with a real "
                "problem description based on what you find and the user's goal.\n\n"
                "Keep the existing Goal / Background / Setup structure. Replace the "
                "placeholder text with concrete details. Be specific and concise.\n\n"
                "If you can't determine something, leave a [TODO: ...] marker.\n\n"
                f"Edit the file: {active_prompt}"
            )
            result = subprocess.run(
                [claude_bin, "--dangerously-skip-permissions", "-p", prefill_prompt],
                cwd=str(repo),
            )
            if result.returncode == 0:
                print(f"\n  {_green(chr(10003))} Claude pre-filled PROMPT.md — review and adjust as needed")
            else:
                print(f"\n  {_yellow('!')} Claude exited with code {result.returncode} — check PROMPT.md manually")
        else:
            print(f"  {_dim('-')} Skipped — edit PROMPT.md manually")

    # 8. Next steps ----------------------------------------------------------
    print(f"\n{_bold('Next steps:')}\n")
    print(f"  1. Review {_blue('PROMPT.md')}")
    print(f"  2. Start the server:")
    print(f"     {_dim('$')} {_green('the-lab .')}")
    print(f"  3. Launch an agent:")
    print(f"     {_dim('$')} {_green('the-lab-agent')}")
    print(f"  4. Open the dashboard at {_blue('http://localhost:8000')}")
    print()


def _find_dashboard_dir() -> Path | None:
    """Find the dashboard/ source directory (for Vite dev server)."""
    # Check relative to the package
    pkg_dir = Path(__file__).parent.parent / "dashboard"
    if (pkg_dir / "package.json").exists():
        return pkg_dir
    return None


def _find_node() -> str | None:
    """Find a Node.js binary >= 18, checking nvm paths first."""
    nvm_dir = os.environ.get("NVM_DIR", str(Path.home() / ".nvm"))
    versions_dir = Path(nvm_dir) / "versions" / "node"
    if versions_dir.exists():
        # Sort descending so we pick the newest version
        for d in sorted(versions_dir.iterdir(), reverse=True):
            node_bin = d / "bin" / "node"
            if node_bin.exists():
                try:
                    v = subprocess.run(
                        [str(node_bin), "--version"],
                        capture_output=True, text=True, timeout=5,
                    )
                    major = int(v.stdout.strip().lstrip("v").split(".")[0])
                    if major >= 18:
                        return str(node_bin)
                except Exception:
                    continue
    # Fall back to system node
    system_node = shutil.which("node")
    if system_node:
        try:
            v = subprocess.run(
                [system_node, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            major = int(v.stdout.strip().lstrip("v").split(".")[0])
            if major >= 18:
                return system_node
        except Exception:
            pass
    return None


def _build_dashboard(dashboard_dir: Path) -> bool:
    """Run `npx vite build` if dashboard sources are newer than the build."""
    static_dir = Path(__file__).parent / "static"
    index_html = static_dir / "index.html"

    # Check if build is needed: no index.html, or any source file newer than it
    needs_build = not index_html.exists()
    if not needs_build:
        build_mtime = index_html.stat().st_mtime
        src_dir = dashboard_dir / "src"
        if src_dir.exists():
            for f in src_dir.rglob("*"):
                if f.is_file() and f.stat().st_mtime > build_mtime:
                    needs_build = True
                    break

    if not needs_build:
        return True

    node = _find_node()
    if not node:
        print("\033[33m  Node.js >= 18 not found — skipping dashboard build\033[0m")
        return False

    vite_js = dashboard_dir / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite_js.exists():
        print("\033[33m  vite not in node_modules — skipping dashboard build\033[0m")
        return False

    print("\033[36m  building dashboard...\033[0m", end=" ", flush=True)
    result = subprocess.run(
        [node, str(vite_js), "build"],
        cwd=str(dashboard_dir),
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        print("\033[32mdone\033[0m")
        return True
    else:
        print(f"\033[31mfailed\033[0m\n{result.stderr[:500]}")
        return False


def _start_vite(dashboard_dir: Path, api_port: int) -> subprocess.Popen | None:
    """Start the Vite dev server if node_modules exists."""
    if not (dashboard_dir / "node_modules").exists():
        print(
            f"\033[33m  dashboard/node_modules not found — run 'npm install' in {dashboard_dir}\033[0m"
        )
        print("  Falling back to API-only mode (no HMR)\n")
        return None

    vite_js = dashboard_dir / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite_js.exists():
        print("\033[33m  vite not found in node_modules — Vite HMR disabled\033[0m\n")
        return None

    node = _find_node()
    if not node:
        print("\033[33m  Node.js >= 18 not found — Vite HMR disabled\033[0m")
        print("  Install via nvm: nvm install 24\n")
        return None

    cmd = [node, str(vite_js), "--clearScreen", "false", "--host", "0.0.0.0"]
    env = {**os.environ, "VITE_API_PORT": str(api_port)}
    proc = subprocess.Popen(
        cmd,
        cwd=str(dashboard_dir),
        env=env,
    )
    print(f"  vite:     http://localhost:5173 (HMR)")
    return proc


def _start_http_redirect(host: str, http_port: int, https_port: int):
    """Run a tiny plain-HTTP server (daemon thread) that 308-redirects every
    request to the HTTPS port. Lets people/agents that hit http:// get bounced
    to https:// instead of a confusing TLS connection error. Returns the server
    (kept alive for the process lifetime) or None if the port couldn't bind.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class _Redirect(BaseHTTPRequestHandler):
        def _do(self):
            # Preserve the requested host (sans port), swap in the HTTPS port.
            host_hdr = (self.headers.get("Host") or "").split(":")[0] or "localhost"
            target = f"https://{host_hdr}:{https_port}{self.path}"
            self.send_response(308)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        # All methods redirect; 308 preserves method + body for API clients.
        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _do

        def log_message(self, *args):  # silence per-request logging
            pass

    try:
        srv = ThreadingHTTPServer((host, http_port), _Redirect)
    except OSError as e:
        print(f"[https] could not start HTTP→HTTPS redirect on :{http_port}: {e}", file=sys.stderr)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _ensure_self_signed_cert(repo_dir: Path) -> tuple[str, str]:
    """Generate a self-signed TLS cert if one doesn't exist. Returns (certfile, keyfile)."""
    cert_dir = repo_dir / ".the_lab" / "tls"
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_file = cert_dir / "cert.pem"
    key_file = cert_dir / "key.pem"

    if cert_file.exists() and key_file.exists():
        return str(cert_file), str(key_file)

    import subprocess as _sp
    _sp.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(key_file), "-out", str(cert_file),
        "-days", "365", "-nodes",
        "-subj", "/CN=the-lab-local",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:0.0.0.0",
    ], check=True, capture_output=True)
    print(f"  generated self-signed cert: {cert_file}")
    return str(cert_file), str(key_file)


def cmd_wait():
    """the-lab wait <experiment_label> [--port N] [--timeout N] [--url URL]

    Long-poll until an experiment finishes, then print a compact JSON result
    and exit with code 0 (completed) or 1 (failed/timeout).

    Designed to be run in the background by Claude Code:

        Bash("the-lab wait 3.15 --port 9009", run_in_background=True)

    Claude Code is notified automatically when the command exits, then reads
    the printed JSON to get the final status and metrics.
    """
    import argparse as _ap, json as _json, urllib.request as _urlreq, urllib.error as _urlerr

    p = _ap.ArgumentParser(prog="the-lab wait")
    p.add_argument("label", help="Experiment label (e.g. '3.15') or ID")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--timeout", type=float, default=3600, help="Max seconds to wait (default 3600)")
    p.add_argument("--url", default=None, help="Override API base URL (e.g. http://host:9009/api/v1)")
    args = p.parse_args(sys.argv[2:])

    api_base = args.url or f"http://localhost:{args.port}/api/v1"

    # Build headers the same way the MCP bridge does — pick up agent ID and
    # Basic auth from environment so auth-gated servers work transparently.
    import base64 as _b64
    headers: dict[str, str] = {}
    agent_id = os.environ.get("THE_LAB_AGENT_ID", "").strip()
    if agent_id:
        headers["X-Agent-Id"] = agent_id
    _user = os.environ.get("THE_LAB_USER", "").strip()
    _pw   = os.environ.get("THE_LAB_PASSWORD", "").strip()
    if _user and _pw:
        headers["Authorization"] = "Basic " + _b64.b64encode(
            f"{_user}:{_pw}".encode()
        ).decode()

    def _get(url: str, timeout: float = 15) -> dict:
        req = _urlreq.Request(url, headers=headers)
        with _urlreq.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read())

    # Resolve the experiment label → global ID via the API
    try:
        exp = _get(f"{api_base}/experiments/{args.label}")
        exp_id = exp.get("id") or args.label
    except Exception:
        exp_id = args.label  # pass label directly to /wait

    url = f"{api_base}/wait?experiment_id={exp_id}&timeout={int(args.timeout)}"
    try:
        result = _get(url, timeout=args.timeout + 30)
    except _urlerr.URLError as e:
        print(_json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        print(_json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)

    exp = result.get("experiment") or {}
    status = exp.get("status") or result.get("status") or result.get("event", "unknown")
    out = {
        "status":     status,
        "label":      exp.get("label") or exp.get("id") or exp_id,
        "metrics":    exp.get("metrics"),
        "error":      exp.get("error"),
        "runtime":    exp.get("runtime"),
        "finished_at": exp.get("finished_at"),
    }
    print(_json.dumps(out))
    sys.exit(0 if status == "completed" else 1)


def cmd_messages():
    """the-lab messages [--port N] [--timeout N] [--url URL] [--poll N]

    Long-poll until at least one unread message is available, print them as
    JSON, and exit.  Designed to be run in the background by Claude Code:

        Bash("the-lab messages --port 9009", run_in_background=True)

    Claude Code is notified automatically when the command exits, then reads
    the printed JSON array to get the messages.  Uses THE_LAB_AGENT_ID,
    THE_LAB_USER, and THE_LAB_PASSWORD from the environment when set.
    """
    import argparse as _ap, json as _json, time as _time
    import urllib.request as _urlreq, urllib.error as _urlerr, base64 as _b64

    p = _ap.ArgumentParser(prog="the-lab messages")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--timeout", type=float, default=300, help="Max seconds to wait (default 300)")
    p.add_argument("--poll", type=float, default=3, help="Polling interval in seconds (default 3)")
    p.add_argument("--url", default=None, help="Override API base URL")
    args = p.parse_args(sys.argv[2:])

    api_base = args.url or f"http://localhost:{args.port}/api/v1"

    headers: dict[str, str] = {}
    agent_id = os.environ.get("THE_LAB_AGENT_ID", "").strip()
    if agent_id:
        headers["X-Agent-Id"] = agent_id
    _user = os.environ.get("THE_LAB_USER", "").strip()
    _pw   = os.environ.get("THE_LAB_PASSWORD", "").strip()
    if _user and _pw:
        headers["Authorization"] = "Basic " + _b64.b64encode(
            f"{_user}:{_pw}".encode()
        ).decode()

    def _get(url: str) -> dict:
        req = _urlreq.Request(url, headers=headers)
        with _urlreq.urlopen(req, timeout=15) as r:
            return _json.loads(r.read())

    deadline = _time.monotonic() + args.timeout
    while True:
        try:
            qs = "limit=50&for_me=1" if agent_id else "limit=50"
            data = _get(f"{api_base}/messages?{qs}")
            msgs = data.get("messages", [])
            unread = [m for m in msgs if not m.get("read_by")]
            if unread:
                print(_json.dumps(unread))
                sys.exit(0)
        except _urlerr.URLError as e:
            print(_json.dumps({"error": str(e)}))
            sys.exit(1)
        except Exception as e:
            print(_json.dumps({"error": str(e)}))
            sys.exit(1)

        if _time.monotonic() >= deadline:
            print(_json.dumps([]))
            sys.exit(0)

        _time.sleep(args.poll)


def main():
    # Handle subcommands before argparse (server mode)
    if len(sys.argv) >= 2 and sys.argv[1] == "init":
        target = sys.argv[2] if len(sys.argv) >= 3 else None
        cmd_init(target)
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "wait":
        cmd_wait()
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "messages":
        cmd_messages()
        return

    parser = argparse.ArgumentParser(description="The Lab — Experiment Management API")
    parser.add_argument(
        "repo",
        nargs="?",
        default=".",
        help="Path to the git repository to manage experiments in (default: current directory)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to (default: 8000)")
    parser.add_argument("--dev", action="store_true", help="Development mode: auto-reload on code changes, hold requests during restart")
    parser.add_argument("--https", action="store_true", help="Enable HTTPS with a self-signed certificate")
    parser.add_argument(
        "--http-redirect-port",
        type=int,
        default=None,
        metavar="PORT",
        help="With --https, also listen on this plain-HTTP port and 308-redirect "
             "to the HTTPS port (default: <port>+1; set 0 to disable).",
    )
    parser.add_argument(
        "--perf",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Log every API call's duration+source to a CSV "
             "(default: <repo>/.the_lab/api_perf.csv)",
    )
    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    if not (repo_path / ".git").exists():
        print(f"Error: {repo_path} is not a git repository", file=sys.stderr)
        sys.exit(1)

    from dotenv import load_dotenv
    load_dotenv(repo_path / ".env")
    load_dotenv(Path(__file__).parent.parent / ".env")

    os.environ["THE_LAB_REPO"] = str(repo_path)

    if args.perf is not None:
        perf_path = Path(args.perf).resolve() if args.perf else (repo_path / ".the_lab" / "api_perf.csv")
        perf_path.parent.mkdir(parents=True, exist_ok=True)
        os.environ["THE_LAB_PERF_LOG"] = str(perf_path)
        print(f"[perf] logging every API call to {perf_path}", file=sys.stderr)

    ssl_kwargs = {}
    if args.https:
        cert_file, key_file = _ensure_self_signed_cert(repo_path)
        ssl_kwargs = {"ssl_certfile": cert_file, "ssl_keyfile": key_file}
        scheme = "https"
        # Plain-HTTP → HTTPS redirect on a sibling port (default: <port>+1).
        redirect_port = args.http_redirect_port if args.http_redirect_port is not None else args.port + 1
        if redirect_port and redirect_port != args.port:
            if _start_http_redirect(args.host, redirect_port, args.port):
                print(
                    f"[https] HTTP→HTTPS redirect: http://{args.host}:{redirect_port} "
                    f"→ https://…:{args.port}",
                    file=sys.stderr,
                )
    else:
        scheme = "http"

    save_runtime_info(
        repo_path,
        {
            "api_scheme": scheme,
            "api_host": args.host,
            "api_port": args.port,
        },
    )

    # Announce auth status so operators know whether the UI is open.
    _auth_user = os.environ.get("THE_LAB_USER", "").strip()
    _auth_pass = os.environ.get("THE_LAB_PASSWORD", "").strip()
    if _auth_user and _auth_pass:
        print(f"[auth] HTTP Basic Auth enabled (user: {_auth_user})", file=sys.stderr)
    else:
        print("[auth] No authentication — set THE_LAB_USER + THE_LAB_PASSWORD to enable", file=sys.stderr)

    # Auto-build dashboard if sources changed
    dashboard_dir = _find_dashboard_dir()
    if dashboard_dir:
        _build_dashboard(dashboard_dir)

    if args.dev:
        import asyncio
        import uvicorn
        from .dev_proxy import DevProxy

        watch_dir = Path(__file__).parent
        proxy = DevProxy(
            repo_dir=str(repo_path),
            host=args.host,
            port=args.port,
            watch_dir=watch_dir,
        )

        # Start Vite dev server alongside the backend if dashboard/ exists
        vite_proc = None
        if dashboard_dir:
            vite_proc = _start_vite(dashboard_dir, proxy.internal_port)

        async def run_dev():
            await proxy.run()
            config = uvicorn.Config(
                proxy.asgi_app(),
                host=args.host,
                port=args.port,
                log_level="warning",
                **ssl_kwargs,
            )
            server = uvicorn.Server(config)
            await server.serve()

        try:
            asyncio.run(run_dev())
        finally:
            if vite_proc and vite_proc.poll() is None:
                vite_proc.terminate()
                vite_proc.wait(timeout=5)
    else:
        import uvicorn
        uvicorn.run("the_lab.app:app", host=args.host, port=args.port, **ssl_kwargs)


if __name__ == "__main__":
    main()
