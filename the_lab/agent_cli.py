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
    mcp_config: str | None = None,
) -> list[str]:
    if agent == "claude":
        cmd = [agent_bin]
        if not no_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if model:
            cmd.extend(["--model", model])
        if mcp_config:
            # Write to temp file — --mcp-config is variadic and consumes
            # subsequent args. Use "--" to terminate named args before the
            # positional prompt.
            import tempfile
            mcp_file = Path(tempfile.gettempdir()) / "the-lab-mcp.json"
            mcp_file.write_text(mcp_config)
            cmd.extend(["--mcp-config", str(mcp_file), "--"])
        cmd.append(loop_prompt)
        return cmd

    cmd = [agent_bin, "--yolo"]
    if model:
        cmd.extend(["--model", model])
    cmd.append(loop_prompt)
    return cmd


def main():
    parser = argparse.ArgumentParser(
        description="Launch Claude Code or Codex using a prompt file",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        help="Use 'loop' to run in loop mode. Omit for a single run.",
    )
    parser.add_argument(
        "prompt_file",
        nargs="?",
        default="PROMPT.md",
        help="Path to the prompt file (default: PROMPT.md)",
    )
    parser.add_argument(
        "-d",
        "--duration",
        default="15m",
        help="Loop interval when using 'loop' (default: 15m). Supports: 30s, 5m, 2h, 1d",
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
        "--sandbox",
        action="store_true",
        help="Launch the agent inside a network sandbox (default: off)",
    )
    parser.add_argument(
        "--repo",
        help="Path to the git repository (must match the repo the server was started with). "
             "Defaults to auto-detect from CWD.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port of the Lab API server (default: 8000)",
    )
    args = parser.parse_args()

    # Handle 'loop' subcommand: "the-lab-agent loop [file]" vs "the-lab-agent [file|string]"
    use_loop = False
    inline_prompt = None
    if args.command == "loop":
        use_loop = True
    elif args.command is not None:
        # First positional arg wasn't "loop" — treat it as prompt file or inline string
        if args.prompt_file != "PROMPT.md":
            # Two positional args, first wasn't "loop" — join as inline prompt
            inline_prompt = args.command + " " + args.prompt_file
        elif Path(args.command).is_file():
            args.prompt_file = args.command
        else:
            # Not a file — treat as inline prompt string
            inline_prompt = args.command

    import tempfile as _tempfile

    project_dir = Path.cwd()
    api_base = f"http://localhost:{args.port}/api/v1"
    api_content = _PROMPT_API.read_text().strip() if _PROMPT_API.exists() else ""
    api_header = f"**Lab API base URL:** `{api_base}`\n\nAll API endpoints below are relative to this base URL. Use `curl {api_base}/orient` to get started.\n\n"

    # Read PROMPT.md if it exists (used in both file and inline modes)
    problem_path = project_dir / "PROMPT.md"
    problem_content = problem_path.read_text().strip() if problem_path.exists() else ""

    if inline_prompt:
        # Inline mode: question first, then background (PROMPT.md + API docs)
        parts = [f"**User Question:** {inline_prompt}\n\n---\n\n**Background information below:**"]
        if problem_content:
            parts.append(problem_content)
        parts.append(api_header + api_content)
        generated = "\n\n".join(parts) + "\n"
        fd, tmp_path = _tempfile.mkstemp(prefix="the-lab-prompt-", suffix=".md")
        os.write(fd, generated.encode())
        os.close(fd)
        prompt_path = Path(tmp_path)
        print(f"Inline prompt (API at {api_base})", file=sys.stderr)
    else:
        prompt_path = Path(args.prompt_file)
        if prompt_path.is_file():
            project_dir = prompt_path.parent
            problem_path = project_dir / "PROMPT.md"
            problem_content = problem_path.read_text().strip() if problem_path.exists() else ""

        if problem_content:
            generated = problem_content + "\n\n" + api_header + api_content + "\n"
            fd, tmp_path = _tempfile.mkstemp(prefix="the-lab-prompt-", suffix=".md")
            os.write(fd, generated.encode())
            os.close(fd)
            prompt_path = Path(tmp_path)
            print(f"Generated prompt (API at {api_base})", file=sys.stderr)
        elif not prompt_path.exists():
            print(f"Error: neither PROMPT.md nor {prompt_path} found", file=sys.stderr)
            sys.exit(1)

    agent_bin = _agent_binary(args.agent)
    if not os.path.isfile(agent_bin):
        print(f"Error: '{agent_bin}' not found in PATH.", file=sys.stderr)
        sys.exit(1)

    # Build MCP config if the bridge script exists
    import json as _json
    mcp_config = None
    mcp_script = project_dir / ".claude" / "skills" / "lab_api_mcp.py"
    if not mcp_script.exists():
        # Fall back to package source
        mcp_script = Path(__file__).parent / "agent_skills" / "skills" / "lab_api_mcp.py"
    if mcp_script.exists():
        api_base = f"http://localhost:{args.port}/api/v1"
        mcp_config = _json.dumps({"mcpServers": {"labapi": {
            "command": "python3",
            "args": [str(mcp_script.resolve())],
            "env": {"PYTHONUNBUFFERED": "1", "THE_LAB_API_URL": api_base},
        }}})
        print(f"MCP bridge: labapi → {api_base}", file=sys.stderr)

    # Build the prompt argument for Claude
    if use_loop:
        # /loop references the file so Claude re-reads it each iteration
        agent_prompt = f"/loop {args.duration} {prompt_path.resolve()}"
        print(f"Mode: loop (every {args.duration})", file=sys.stderr)
    else:
        agent_prompt = str(prompt_path.resolve())
        print(f"Mode: single run", file=sys.stderr)

    cmd = _build_launch_command(
        args.agent,
        agent_bin,
        agent_prompt,
        args.model,
        args.no_skip_permissions,
        mcp_config=mcp_config,
    )

    env = dict(os.environ)
    if args.sandbox:
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
