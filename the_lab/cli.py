"""CLI entrypoint for The Lab API server."""
import argparse
import os
import sys
from pathlib import Path


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
    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    if not (repo_path / ".git").exists():
        print(f"Error: {repo_path} is not a git repository", file=sys.stderr)
        sys.exit(1)

    os.environ["THE_LAB_REPO"] = str(repo_path)

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

        async def run_dev():
            await proxy.run()
            config = uvicorn.Config(
                proxy.asgi_app(),
                host=args.host,
                port=args.port,
                log_level="warning",
            )
            server = uvicorn.Server(config)
            await server.serve()

        asyncio.run(run_dev())
    else:
        import uvicorn
        uvicorn.run("the_lab.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
