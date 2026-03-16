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
    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    if not (repo_path / ".git").exists():
        print(f"Error: {repo_path} is not a git repository", file=sys.stderr)
        sys.exit(1)

    # Set env var before importing app (app reads it at module level)
    os.environ["THE_LAB_REPO"] = str(repo_path)

    import uvicorn
    uvicorn.run("the_lab.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
