"""CLI to launch a supported coding agent in loop mode from a prompt file."""

import argparse
import os
import shutil
import sys
from pathlib import Path

from .sandbox import build_sandbox_command, sandbox_capabilities

# PROMPT_api.md ships with the_lab package
_PROMPT_API = Path(__file__).parent / "PROMPT_api.md"


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
    parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="Port of the Lab API server (default: 9000)",
    )
    args = parser.parse_args()

    prompt_path = Path(args.prompt_file)

    # --- Prompt generation ---
    # If PROMPT_problem.md exists, concatenate with PROMPT_api.md → PROMPT_generated.md
    # Otherwise fall back to PROMPT.md (legacy)
    project_dir = prompt_path.parent if prompt_path.is_file() else Path.cwd()
    problem_path = project_dir / "PROMPT_problem.md"
    generated_path = project_dir / "PROMPT_generated.md"

    if problem_path.exists():
        problem_content = problem_path.read_text().strip()
        api_content = _PROMPT_API.read_text().strip() if _PROMPT_API.exists() else ""
        api_base = f"http://localhost:{args.port}/api/v1"
        api_header = f"**Lab API base URL:** `{api_base}`\n\nAll API endpoints below are relative to this base URL. Use `curl {api_base}/orient` to get started.\n\n"
        generated = problem_content + "\n\n" + api_header + api_content + "\n"
        generated_path.write_text(generated)
        prompt_path = generated_path
        print(f"Generated {generated_path.name} (API at {api_base})", file=sys.stderr)
    elif not prompt_path.exists():
        print(f"Error: neither PROMPT_problem.md nor {prompt_path} found", file=sys.stderr)
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
                "Error: could not find the repo root for the sandboxed agent launch. "
                "Run from your git repo or pass --no-sandbox.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Always sandbox the agent — the config "enabled" flag only controls
        # experiment sandboxing (server-side).  Use --no-sandbox to skip.
        capabilities = sandbox_capabilities()
        if not capabilities.get("available"):
            details = capabilities.get("details") or "sandbox runtime unavailable"
            print(f"Error: sandbox is unavailable: {details}", file=sys.stderr)
            sys.exit(1)
        env["THE_LAB_SANDBOX_TARGET_UID"] = str(os.getuid())
        env["THE_LAB_SANDBOX_TARGET_GID"] = str(os.getgid())
        cmd = build_sandbox_command(repo_root, args.agent, prompt_path.name, cmd)

    os.execvpe(cmd[0], cmd, env)


if __name__ == "__main__":
    main()
