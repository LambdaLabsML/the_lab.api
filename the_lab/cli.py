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
# Template for PROMPT_problem.md
# ---------------------------------------------------------------------------

TEMPLATE_PROMPT = """\
# [Your Research Goal]

## Goal
Describe what you're optimizing and the success criteria.
Example: Maximize accuracy on held-out evaluation set while minimizing compute cost.

## Background
Prior work, constraints, relevant context.
Example: We have a baseline model achieving 10% accuracy. The framework supports N-agent collaboration.

## Setup
- Hardware: [describe your setup, e.g. 8xH100 node]
- Data: [where is your data, e.g. /data/heldout/*.json]
- Run: [how to run experiments, e.g. python run.py --config <path>]
"""

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

    # 2. PROMPT_problem.md ---------------------------------------------------
    prompt_path = repo / "PROMPT_problem.md"
    if prompt_path.exists():
        print(f"  {_green(chr(10003))} PROMPT_problem.md already exists")
    else:
        prompt_path.write_text(TEMPLATE_PROMPT)
        print(f"  {_green(chr(10003))} Created PROMPT_problem.md -- edit this with your research problem")

    # 3. MCP bridge ----------------------------------------------------------
    _pkg_skills = Path(__file__).parent.parent / "agent_skills"
    mcp_script_src = _pkg_skills / "skills" / "lab_api_mcp.py"
    mcp_json_src = _pkg_skills / "mcp.json"

    if mcp_script_src.exists():
        mcp_dst = repo / ".claude" / "skills" / "lab_api_mcp.py"
        mcp_json_dst = repo / ".mcp.json"
        if mcp_dst.exists() and mcp_json_dst.exists():
            print(f"  {_green(chr(10003))} MCP bridge already installed")
        elif _ask_yn("  Install MCP bridge? (lets agents use typed tool calls instead of curl)"):
            mcp_dst.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(mcp_script_src, mcp_dst)
            if mcp_json_src.exists():
                shutil.copy2(mcp_json_src, mcp_json_dst)
            print(f"  {_green(chr(10003))} Installed MCP bridge (.mcp.json + .claude/skills/lab_api_mcp.py)")
        else:
            print(f"  {_dim('-')} Skipped MCP bridge")
    else:
        print(f"  {_dim('-')} MCP bridge not found in package (agent_skills/ missing)")

    # 4. .gitignore ----------------------------------------------------------
    gitignore = repo / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    lines = existing.splitlines()

    entries_to_add = []
    for entry in [".the_lab/", ".claude/", ".mcp.json", "PROMPT_problem.md", "PROMPT_generated.md"]:
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

    # 5. Next steps ----------------------------------------------------------
    print(f"\n{_bold('Next steps:')}\n")
    print(f"  1. Edit {_blue('PROMPT_problem.md')} with your research problem")
    print(f"  2. Start the server:")
    print(f"     {_dim('$')} {_green('the-lab')} {repo}")
    print(f"  3. Launch an agent:")
    print(f"     {_dim('$')} {_green('the-lab-agent')} PROMPT_problem.md")
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

    cmd = [node, str(vite_js), "--clearScreen", "false"]
    env = {**os.environ, "VITE_API_PORT": str(api_port)}
    proc = subprocess.Popen(
        cmd,
        cwd=str(dashboard_dir),
        env=env,
    )
    print(f"  vite:     http://localhost:5173 (HMR)")
    return proc


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


def main():
    # Handle 'init' subcommand before argparse (server mode)
    if len(sys.argv) >= 2 and sys.argv[1] == "init":
        target = sys.argv[2] if len(sys.argv) >= 3 else None
        cmd_init(target)
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
    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    if not (repo_path / ".git").exists():
        print(f"Error: {repo_path} is not a git repository", file=sys.stderr)
        sys.exit(1)

    from dotenv import load_dotenv
    load_dotenv(repo_path / ".env")
    load_dotenv(Path(__file__).parent.parent / ".env")

    os.environ["THE_LAB_REPO"] = str(repo_path)

    ssl_kwargs = {}
    if args.https:
        cert_file, key_file = _ensure_self_signed_cert(repo_path)
        ssl_kwargs = {"ssl_certfile": cert_file, "ssl_keyfile": key_file}
        scheme = "https"
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
