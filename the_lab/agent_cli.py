"""CLI to launch a supported coding agent in loop mode from a prompt file."""

import argparse
import os
import shutil
import sys
from pathlib import Path

from .sandbox import build_sandbox_command, load_sandbox_config, sandbox_capabilities


def _find_repo_root(*paths: Path) -> Path | None:
    for path in paths:
        cur = path.resolve()
        if cur.is_file():
            cur = cur.parent
        for candidate in (cur, *cur.parents):
            if (candidate / ".git").exists():
                return candidate
    return None


def _agent_binary(agent: str) -> str:
    name = "claude" if agent == "claude" else "codex"
    # Resolve to absolute path so the binary is reachable inside the sandbox
    # where PATH may differ from the host environment.
    return shutil.which(name) or name


def _build_launch_command(
    agent: str,
    agent_bin: str,
    loop_prompt: str,
    model: str | None,
    no_skip_permissions: bool,
) -> list[str]:
    if agent == "claude":
        cmd = [agent_bin]
        if not no_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if model:
            cmd.extend(["--model", model])
        cmd.append(loop_prompt)
        return cmd

    cmd = [agent_bin, "--yolo"]
    if model:
        cmd.extend(["--model", model])
    cmd.append(loop_prompt)
    return cmd


def main():
    parser = argparse.ArgumentParser(
        description="Launch Claude Code or Codex in loop mode using a prompt file",
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
        "--agent",
        choices=["claude", "codex"],
        default="claude",
        help="Which interactive agent CLI to launch (default: claude)",
    )
    parser.add_argument(
        "--model",
        help="Model to use for the selected agent CLI",
    )
    parser.add_argument(
        "--no-skip-permissions",
        action="store_true",
        help="Claude only: don't pass --dangerously-skip-permissions (it is on by default)",
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="Launch the agent directly without the network sandbox",
    )
    parser.add_argument(
        "--repo",
        help="Path to the git repository (must match the repo the server was started with). "
             "Defaults to auto-detect from CWD.",
    )
    args = parser.parse_args()

    prompt_path = Path(args.prompt_file)
    if not prompt_path.exists():
        print(f"Error: {prompt_path} not found", file=sys.stderr)
        sys.exit(1)

    agent_bin = _agent_binary(args.agent)
    if not os.path.isfile(agent_bin):
        print(f"Error: '{agent_bin}' not found in PATH.", file=sys.stderr)
        sys.exit(1)

    content = prompt_path.read_text().strip()
    loop_prompt = f"/loop {args.duration} {content}"
    cmd = _build_launch_command(
        args.agent,
        agent_bin,
        loop_prompt,
        args.model,
        args.no_skip_permissions,
    )

    env = dict(os.environ)
    if not args.no_sandbox:
        if args.repo:
            repo_root = Path(args.repo).resolve()
            if not (repo_root / ".git").exists():
                print(f"Error: {repo_root} is not a git repository", file=sys.stderr)
                sys.exit(1)
        else:
            repo_root = _find_repo_root(Path.cwd(), prompt_path.parent)
        if repo_root is None:
            print(
                "Error: could not find the repo root for the sandboxed Claude launch. "
                "Run from your git repo or pass --no-sandbox.",
                file=sys.stderr,
            )
            sys.exit(1)
        config = load_sandbox_config(repo_root)
        if config.get("enabled", True):
            capabilities = sandbox_capabilities()
            if not capabilities.get("available"):
                details = capabilities.get("details") or "sandbox runtime unavailable"
                print(f"Error: sandbox is enabled but unavailable: {details}", file=sys.stderr)
                sys.exit(1)
            env["THE_LAB_SANDBOX_TARGET_UID"] = str(os.getuid())
            env["THE_LAB_SANDBOX_TARGET_GID"] = str(os.getgid())
            cmd = build_sandbox_command(repo_root, args.agent, prompt_path.name, cmd)

    os.execvpe(cmd[0], cmd, env)


if __name__ == "__main__":
    main()
