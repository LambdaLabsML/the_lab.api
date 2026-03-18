"""CLI to launch a Claude Code agent in loop mode from a prompt file."""

import argparse
import os
import shutil
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Launch a Claude Code agent in loop mode using a prompt file",
    )
    parser.add_argument(
        "prompt_file",
        nargs="?",
        default="LOOP.md",
        help="Path to the loop prompt file (default: LOOP.md)",
    )
    parser.add_argument(
        "-d",
        "--duration",
        default="15m",
        help="Loop interval (default: 15m). Supports: 30s, 5m, 2h, 1d",
    )
    parser.add_argument(
        "--model",
        help="Model to use (e.g. sonnet, opus, claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--no-skip-permissions",
        action="store_true",
        help="Don't pass --dangerously-skip-permissions (it is on by default)",
    )
    args = parser.parse_args()

    prompt_path = Path(args.prompt_file)
    if not prompt_path.exists():
        print(f"Error: {prompt_path} not found", file=sys.stderr)
        sys.exit(1)

    if not shutil.which("claude"):
        print("Error: 'claude' not found in PATH. Install Claude Code first.", file=sys.stderr)
        sys.exit(1)

    content = prompt_path.read_text().strip()
    loop_prompt = f"/loop {args.duration} {content}"

    cmd = ["claude"]
    if not args.no_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if args.model:
        cmd.extend(["--model", args.model])
    cmd.append(loop_prompt)

    os.execvp("claude", cmd)


if __name__ == "__main__":
    main()
