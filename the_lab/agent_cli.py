"""CLI to launch a supported coding agent in loop mode from a prompt file."""

import argparse
import os
import shutil
import sys
from pathlib import Path

from .sandbox import build_sandbox_command, load_sandbox_config, sandbox_capabilities

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
    mcp_path: str | None = None,
) -> list[str]:
    """Build the agent launch command.

    mcp_config: raw JSON content — written to mcp_path (or a temp file if
                mcp_path is None) and passed via --mcp-config.
    mcp_path:   destination path for the MCP config file.  Caller is
                responsible for ensuring this path is visible inside the
                sandbox (write it there, or inject it via bwrap --file).
    """
    if agent == "claude":
        cmd = [agent_bin]
        if not no_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if model:
            cmd.extend(["--model", model])
        if mcp_config:
            import tempfile
            if mcp_path is None:
                dest = Path(tempfile.gettempdir()) / "the-lab-mcp.json"
                dest.write_text(mcp_config)
                mcp_path = str(dest)
            cmd.extend(["--mcp-config", mcp_path, "--"])
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
        nargs="?",
        const="on",
        default="auto",
        metavar="on|off|auto",
        help="Launch the agent inside a network sandbox. "
             "'auto' (default) reads the enabled flag from .the_lab/sandbox/config.json",
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
    parser.add_argument(
        "--role",
        default=None,
        help="Role-specific prompt to use (looks up .the_lab/PROMPT.<role>.md). "
             "Falls back to the default prompt if the role has no file. "
             "Run with --list-roles to see what's configured.",
    )
    parser.add_argument(
        "--list-roles",
        action="store_true",
        help="List configured prompt roles (from .the_lab/PROMPT*.md) and exit.",
    )
    parser.add_argument(
        "--no-isolated",
        action="store_true",
        help="Run in the main repo instead of registering a per-agent git "
             "worktree (legacy behaviour). Default is isolated mode: a fresh "
             "worktree is created and removed automatically around the run.",
    )
    args = parser.parse_args()

    # --list-roles: handle and exit before any agent setup.
    if args.list_roles:
        repo_root = _find_repo_root(Path.cwd()) or Path.cwd()
        from .prompts import list_roles as _list_roles
        rows = _list_roles(repo_root)
        if not rows:
            print("No prompt roles configured yet.")
            print(f"Run `the-lab init` in {repo_root} or add .the_lab/PROMPT.md directly.")
        else:
            print(f"Configured roles in {repo_root}/.the_lab/:")
            for r in rows:
                print(f"  {r['role']:<20}  {r['size']:>6} bytes   updated {r['updated_at'][:19]}")
        sys.exit(0)

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

    # Resolve role → concrete prompt content.
    # Look under <repo>/.the_lab/PROMPT.<role>.md; fall back to the default
    # (<repo>/.the_lab/PROMPT.md, then repo-root PROMPT.md for legacy projects).
    repo_root_for_prompts = _find_repo_root(Path.cwd()) or Path.cwd()
    from .prompts import DEFAULT_ROLE, read_prompt
    effective_role = DEFAULT_ROLE
    problem_content = ""
    if args.role:
        role_content = read_prompt(repo_root_for_prompts, args.role)
        if role_content is not None:
            effective_role = args.role
            problem_content = role_content.strip()
        else:
            print(
                f"[warn] role '{args.role}' not found; falling back to default",
                file=sys.stderr,
            )
            default_content = read_prompt(repo_root_for_prompts, DEFAULT_ROLE)
            if default_content is not None:
                problem_content = default_content.strip()
    else:
        default_content = read_prompt(repo_root_for_prompts, DEFAULT_ROLE)
        if default_content is not None:
            problem_content = default_content.strip()

    # Legacy single-file path used by the "prompt file" positional arg below.
    problem_path = project_dir / "PROMPT.md"

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

    # Build MCP config if the bridge script exists. The packaged bridge is
    # the source of truth — local copies under ``<project>/.claude/skills/``
    # silently rot (they predate X-Agent-Id, the anyOf flatten fix, etc.) and
    # break agent isolation when picked up by accident. Set
    # THE_LAB_LOCAL_MCP=1 to opt back into the local copy.
    import json as _json
    mcp_config = None
    mcp_script = Path(__file__).parent / "agent_skills" / "skills" / "lab_api_mcp.py"
    if os.environ.get("THE_LAB_LOCAL_MCP") == "1":
        local = project_dir / ".claude" / "skills" / "lab_api_mcp.py"
        if local.exists():
            mcp_script = local
            print(f"MCP bridge: using local copy at {local} (THE_LAB_LOCAL_MCP=1)", file=sys.stderr)
    if mcp_script.exists():
        api_base = f"http://localhost:{args.port}/api/v1"
        # Thread auth credentials through to the bridge so it can attach
        # Authorization headers when the API has Basic Auth enabled.
        _bridge_env: dict[str, str] = {"PYTHONUNBUFFERED": "1", "THE_LAB_API_URL": api_base}
        for _k in ("THE_LAB_USER", "THE_LAB_PASSWORD"):
            _v = os.environ.get(_k, "").strip()
            if _v:
                _bridge_env[_k] = _v
        mcp_config = _json.dumps({"mcpServers": {"labapi": {
            "command": "python3",
            "args": [str(mcp_script.resolve())],
            "env": _bridge_env,
        }}})
        print(f"MCP bridge: labapi → {api_base}", file=sys.stderr)

    # Build the prompt argument for Claude
    role_suffix = f" (role='{effective_role}')" if effective_role != "default" else ""
    role_arg = f"role='{effective_role}'" if effective_role != "default" else ""
    if use_loop:
        # In loop mode, ask the model to re-read instructions each iteration
        tool_call = f"get_instructions tool with {role_arg}" if role_arg else "get_instructions tool"
        agent_prompt = f"/loop {args.duration} Please re-read the instructions provided by thelabapi ({tool_call}) and continue working on the provided problem."
        print(f"Mode: loop (every {args.duration}){role_suffix}", file=sys.stderr)
    else:
        # Prepend a directive to call get_instructions first if MCP is available
        if mcp_config:
            call = f"get_instructions({role_arg})" if role_arg else "get_instructions()"
            preamble = f"Please start by calling {call} to load the current task and API reference, then proceed to work on the problem.\n\n"
            # Write a new temp file with the preamble + original prompt content
            original_content = prompt_path.read_text()
            fd2, tmp2 = _tempfile.mkstemp(prefix="the-lab-prompt-", suffix=".md")
            os.write(fd2, (preamble + original_content).encode())
            os.close(fd2)
            prompt_path = Path(tmp2)
        agent_prompt = str(prompt_path.resolve())
        print(f"Mode: single run{role_suffix}", file=sys.stderr)

    # Resolve sandbox mode and repo_root early so we can pass mcp_dir to
    # _build_launch_command (the sandbox replaces /tmp with a fresh tmpfs,
    # so the MCP config must live inside the repo's bound sandbox dir instead).
    sandbox_mode = args.sandbox
    if sandbox_mode == "auto":
        _auto_root = (
            Path(args.repo).resolve() if args.repo
            else _find_repo_root(Path.cwd(), prompt_path.parent)
        )
        if _auto_root and (_auto_root / ".git").exists():
            sandbox_mode = "on" if load_sandbox_config(_auto_root).get("enabled", False) else None
        else:
            sandbox_mode = None
    if sandbox_mode in ("on", "off"):
        sandbox_mode = sandbox_mode == "on"
    elif sandbox_mode is not None:
        sandbox_mode = bool(sandbox_mode)

    # Determine repo_root now (needed for sandbox + mcp_dir).
    repo_root: Path | None = None
    if sandbox_mode:
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
                "Run from your git repo.",
                file=sys.stderr,
            )
            sys.exit(1)

    # When sandboxed, inject the MCP config via bwrap --file FD DEST so it
    # lands in the sandbox's tmpfs (/tmp) — NFS paths are inaccessible to the
    # sandbox's mapped sub-UID even with 644 permissions (user namespace UID
    # remapping causes NFS to deny access).
    mcp_pipe_fd: int | None = None
    mcp_path_in_sandbox: str | None = None
    extra_bwrap: list[str] = []

    if sandbox_mode and mcp_config:
        import fcntl
        r_fd, w_fd = os.pipe()
        os.write(w_fd, mcp_config.encode())
        os.close(w_fd)
        # Clear FD_CLOEXEC so the read-end survives the exec into bwrap/rootlesskit.
        flags = fcntl.fcntl(r_fd, fcntl.F_GETFD)
        fcntl.fcntl(r_fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
        mcp_pipe_fd = r_fd
        mcp_path_in_sandbox = "/tmp/the-lab-mcp.json"
        # bwrap reads from r_fd and creates the file at mcp_path_in_sandbox
        # inside the sandbox's fresh tmpfs — no NFS involved at all.
        extra_bwrap = ["--file", str(r_fd), mcp_path_in_sandbox]

    cmd = _build_launch_command(
        args.agent,
        agent_bin,
        agent_prompt,
        args.model,
        args.no_skip_permissions,
        mcp_config=mcp_config,
        mcp_path=mcp_path_in_sandbox,  # None when not sandboxed → writes to /tmp itself
    )

    env = dict(os.environ)

    # Isolated mode (default): register a per-agent worktree with the API
    # server and run the agent inside it. The X-Agent-Id is propagated to
    # the MCP bridge via env so every API call routes to this worktree.
    agent_id: str | None = None
    agent_worktree: Path | None = None
    if not args.no_isolated:
        api_base_for_register = f"http://localhost:{args.port}/api/v1"
        try:
            import urllib.request as _urlreq
            import urllib.error as _urlerr
            import json as _json
            payload = _json.dumps({
                "role": args.role,
                "pid": os.getpid(),
            }).encode()
            _reg_headers: dict[str, str] = {"Content-Type": "application/json"}
            _lab_user = os.environ.get("THE_LAB_USER", "").strip()
            _lab_pw   = os.environ.get("THE_LAB_PASSWORD", "").strip()
            if _lab_user and _lab_pw:
                import base64 as _b64
                _reg_headers["Authorization"] = "Basic " + _b64.b64encode(
                    f"{_lab_user}:{_lab_pw}".encode()
                ).decode()
            req_obj = _urlreq.Request(
                f"{api_base_for_register}/agents/register",
                data=payload, method="POST",
                headers=_reg_headers,
            )
            with _urlreq.urlopen(req_obj, timeout=10) as resp:
                reg = _json.loads(resp.read().decode())
            agent_id = reg["agent_id"]
            agent_worktree = Path(reg["worktree"]).resolve()
            print(
                f"Agent: registered id={agent_id} on branch {reg['branch']} "
                f"(parent: {reg['parent_branch']})\n"
                f"  worktree: {agent_worktree}",
                file=sys.stderr,
            )
            env["THE_LAB_AGENT_ID"] = agent_id
            env["THE_LAB_AGENT_WORKTREE"] = str(agent_worktree)
        except Exception as e:
            _YELLOW = "\033[33m"
            _BOLD   = "\033[1m"
            _RESET  = "\033[0m"
            print(
                f"\n{_BOLD}{_YELLOW}⚠  Agent registration failed{_RESET}\n"
                f"   {e}\n"
                f"   Running in legacy/main-repo mode (no isolated worktree).\n"
                f"   Is the Lab API server running? Pass --no-isolated to silence.\n",
                file=sys.stderr,
            )

    if sandbox_mode:
        # Capabilities check deferred to here so it only runs when needed.
        capabilities = sandbox_capabilities()
        if not capabilities.get("available"):
            details = capabilities.get("details") or "sandbox runtime unavailable"
            print(f"Error: sandbox is unavailable: {details}", file=sys.stderr)
            sys.exit(1)
        env["THE_LAB_SANDBOX_TARGET_UID"] = str(os.getuid())
        env["THE_LAB_SANDBOX_TARGET_GID"] = str(os.getgid())
        cmd = build_sandbox_command(
            repo_root, args.agent, prompt_path.name, cmd,
            cwd=os.getcwd(),
            extra_bwrap_args=extra_bwrap or None,
        )

    # Pick the cwd for the agent process: its worktree if isolated, else
    # whatever cwd we were launched from.
    run_cwd = str(agent_worktree) if agent_worktree else None

    # In isolated mode we need to outlive the agent process so we can
    # unregister + clean up the worktree. Use Popen + signal forwarding
    # instead of execvpe.
    if agent_id:
        import signal as _signal
        import subprocess as _sp
        proc = _sp.Popen(cmd, env=env, cwd=run_cwd)
        forwarded = {"sig": None}

        def _forward(signum, _frame):
            forwarded["sig"] = signum
            try:
                proc.send_signal(signum)
            except Exception:
                pass

        for s in (_signal.SIGINT, _signal.SIGTERM, _signal.SIGHUP):
            try:
                _signal.signal(s, _forward)
            except (ValueError, OSError):
                pass

        try:
            rc = proc.wait()
        finally:
            try:
                import urllib.request as _urlreq, base64 as _b64
                _del_headers: dict[str, str] = {}
                _lu = os.environ.get("THE_LAB_USER", "").strip()
                _lp = os.environ.get("THE_LAB_PASSWORD", "").strip()
                if _lu and _lp:
                    _del_headers["Authorization"] = "Basic " + _b64.b64encode(
                        f"{_lu}:{_lp}".encode()
                    ).decode()
                _urlreq.urlopen(
                    _urlreq.Request(
                        f"http://localhost:{args.port}/api/v1/agents/{agent_id}",
                        method="DELETE", headers=_del_headers,
                    ),
                    timeout=10,
                )
            except Exception as e:
                print(f"Warning: failed to unregister agent {agent_id}: {e}",
                      file=sys.stderr)
        sys.exit(rc)

    # Legacy / --no-isolated path: replace this process with the agent.
    os.execvpe(cmd[0], cmd, env)


if __name__ == "__main__":
    main()
