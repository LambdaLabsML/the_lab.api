"""CLI entrypoint for The Lab API server."""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


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

    # Load .env from the repo dir, then from the package dir (repo takes precedence)
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
        dashboard_dir = _find_dashboard_dir()
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
