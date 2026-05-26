"""Network sandbox configuration and launch helpers."""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_PACKAGE_HOSTS = [
    "pypi.org",
    "files.pythonhosted.org",
    "pythonhosted.org",
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "deb.debian.org",
]

# Hosts that agent CLIs (Claude Code, Codex) need to function.
DEFAULT_AGENT_HOSTS = [
    "*.anthropic.com",
    "*.claude.ai",
    "platform.claude.com",
    "*.openai.com",
    "*.googleapis.com",
    "huggingface.co",
    "*.huggingface.co",
    "*.hf.co",
    "*.sentry.io",
    "*.datadoghq.com",
    "sentry.io",
]

REQUIRED_BINARIES = [
    "rootlesskit",
    "slirp4netns",
    "iptables",
    "ip",
    "bwrap",
]


# System paths always bound read-only inside the sandbox (not user-configurable).
# Missing entries are skipped silently — some distros lack /lib64, etc.
_SYSTEM_RO_BINDS = [
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc",
    "/run",
    "/opt",
    "/var/lib/dpkg",   # apt/dpkg metadata; read-only is fine
]


def sandbox_dir(repo_dir: Path) -> Path:
    path = repo_dir / ".the_lab" / "sandbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sandbox_config_path(repo_dir: Path) -> Path:
    return sandbox_dir(repo_dir) / "config.json"


def sandbox_access_log_path(repo_dir: Path) -> Path:
    return sandbox_dir(repo_dir) / "access.jsonl"


def sandbox_runtime_path(repo_dir: Path) -> Path:
    return sandbox_dir(repo_dir) / "runtime.json"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def get_disable_password_hash(repo_dir: Path) -> str | None:
    stored = _read_json(sandbox_config_path(repo_dir), {})
    return stored.get("disable_password_hash") or None


def set_disable_password(repo_dir: Path, password: str) -> None:
    stored = _read_json(sandbox_config_path(repo_dir), {})
    stored["disable_password_hash"] = _hash_password(password)
    _write_json(sandbox_config_path(repo_dir), stored)


def verify_disable_password(repo_dir: Path, password: str) -> bool:
    pw_hash = get_disable_password_hash(repo_dir)
    if not pw_hash:
        return True  # no password set — allow freely
    return hmac.compare_digest(pw_hash, _hash_password(password))


def _normalize_rule(rule: str) -> str:
    value = rule.strip().lower()
    if not value or value.startswith("#"):
        return ""
    if "://" in value:
        parsed = urlparse(value)
        value = parsed.hostname or value
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value.rstrip(".")


def normalize_rules(values: list[str] | None) -> list[str]:
    if not values:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        for line in str(value).splitlines():
            rule = _normalize_rule(line)
            if rule and rule not in seen:
                seen.add(rule)
                result.append(rule)
    return result


def _normalize_path(raw: str, base_dir: str | None = None) -> str:
    """Normalize a file-rule path: strip comments, expanduser, absolute only.

    Relative paths are resolved against *base_dir* (typically repo_dir) when
    provided, so callers can use paths like ``results/`` or ``./src``.
    """
    value = raw.strip()
    if not value or value.startswith("#"):
        return ""
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        if base_dir:
            value = os.path.join(base_dir, value)
        else:
            return ""
    # Collapse trailing slashes (except on bare /)
    while len(value) > 1 and value.endswith("/"):
        value = value[:-1]
    return value


def normalize_paths(values: list[str] | None, base_dir: str | None = None) -> list[str]:
    if not values:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        for line in str(value).splitlines():
            path = _normalize_path(line, base_dir=base_dir)
            if path and path not in seen:
                seen.add(path)
                result.append(path)
    return result


def _hosts_from_urls(values: list[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        for token in str(value).split():
            if "://" not in token:
                continue
            try:
                parsed = urlparse(token)
            except ValueError:
                continue
            if parsed.hostname:
                found.append(parsed.hostname)
    return found


def _env_default_hosts() -> list[str]:
    env_keys = [
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_FIND_LINKS",
        "PIP_TRUSTED_HOST",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
        "UV_DEFAULT_INDEX",
    ]
    hosts: list[str] = []
    for key in env_keys:
        value = os.environ.get(key)
        if not value:
            continue
        if key.endswith("TRUSTED_HOST"):
            hosts.extend(value.split())
        else:
            hosts.extend(_hosts_from_urls([value]))
    return hosts


def _apt_default_hosts() -> list[str]:
    hosts: list[str] = []
    candidate_paths = [
        Path("/etc/apt/sources.list"),
        *sorted(Path("/etc/apt/sources.list.d").glob("*.list")),
        *sorted(Path("/etc/apt/sources.list.d").glob("*.sources")),
    ]
    for path in candidate_paths:
        if not path.exists():
            continue
        try:
            content = path.read_text()
        except OSError:
            continue
        for token in content.split():
            if token.startswith(("http://", "https://")):
                hosts.extend(_hosts_from_urls([token]))
    return hosts


def builtin_allowlist(repo_dir: Path) -> list[str]:
    del repo_dir  # reserved for future repo-specific defaults
    rules = normalize_rules([
        *DEFAULT_PACKAGE_HOSTS,
        *DEFAULT_AGENT_HOSTS,
        *_env_default_hosts(),
        *_apt_default_hosts(),
    ])
    return rules


def default_file_rw(repo_dir: Path) -> list[str]:
    """Default RW bind-mounts for agents: the repo and agent credentials."""
    home = Path(os.path.expanduser("~"))
    candidates = [
        str(repo_dir),
        str(home / ".claude"),
        str(home / ".claude.json"),
        str(home / ".codex"),
    ]
    return normalize_paths(candidates)


def default_file_ro(repo_dir: Path) -> list[str]:
    """Default RO bind-mounts: user's local bin, agent configs, node runtime."""
    del repo_dir
    home = Path(os.path.expanduser("~"))
    candidates = [
        str(home / ".local"),
        str(home / ".config"),
        str(home / ".gitconfig"),
        str(home / ".gitignore_global"),
    ]
    # Auto-detect nvm node install so Claude Code can spawn node.
    nvm_dir = home / ".nvm" / "versions" / "node"
    if nvm_dir.exists():
        for entry in nvm_dir.iterdir():
            if entry.is_dir():
                candidates.append(str(entry))
    return normalize_paths(candidates)


def builtin_file_binds() -> list[dict]:
    """System paths bound read-only in every sandbox. UI-visible but read-only."""
    rows = []
    for path in _SYSTEM_RO_BINDS:
        if Path(path).exists():
            rows.append({"path": path, "mode": "ro"})
    return rows


def default_sandbox_config(repo_dir: Path) -> dict:
    return {
        "enabled": False,
        "mode": "default-deny",
        "allowlist": [],
        "denylist": [],
        "file_rw": [],
        "file_ro": [],
        "builtin_allowlist": builtin_allowlist(repo_dir),
        "builtin_file_rw": default_file_rw(repo_dir),
        "builtin_file_ro": default_file_ro(repo_dir),
        "builtin_file_binds": builtin_file_binds(),
    }


def load_sandbox_config(repo_dir: Path) -> dict:
    stored = _read_json(sandbox_config_path(repo_dir), {})
    config = default_sandbox_config(repo_dir)
    base = str(repo_dir)
    config["enabled"] = bool(stored.get("enabled", False))
    config["allowlist"] = normalize_rules(stored.get("allowlist", []))
    config["denylist"] = normalize_rules(stored.get("denylist", []))
    config["file_rw"] = normalize_paths(stored.get("file_rw", []), base_dir=base)
    config["file_ro"] = normalize_paths(stored.get("file_ro", []), base_dir=base)
    config["has_disable_password"] = bool(stored.get("disable_password_hash"))
    return config


def save_sandbox_config(repo_dir: Path, payload: dict) -> dict:
    base = str(repo_dir)
    stored = {
        "enabled": bool(payload.get("enabled", True)),
        "allowlist": normalize_rules(payload.get("allowlist", [])),
        "denylist": normalize_rules(payload.get("denylist", [])),
        "file_rw": normalize_paths(payload.get("file_rw", []), base_dir=base),
        "file_ro": normalize_paths(payload.get("file_ro", []), base_dir=base),
    }
    _write_json(sandbox_config_path(repo_dir), stored)
    return load_sandbox_config(repo_dir)


def _matches_rule(rule: str, host: str | None, ip: str | None) -> bool:
    rule = _normalize_rule(rule)
    host = _normalize_rule(host or "")
    ip = _normalize_rule(ip or "")
    if not rule:
        return False
    if host:
        if host == rule or host.endswith("." + rule):
            return True
        if fnmatch.fnmatch(host, rule):
            return True
    if ip and (ip == rule or fnmatch.fnmatch(ip, rule)):
        return True
    return False


def decide_access(config: dict, host: str | None, ip: str | None) -> tuple[bool, str]:
    for rule in config.get("denylist", []):
        if _matches_rule(rule, host, ip):
            return False, f"deny:{rule}"
    for rule in config.get("builtin_allowlist", []):
        if _matches_rule(rule, host, ip):
            return True, f"builtin:{rule}"
    for rule in config.get("allowlist", []):
        if _matches_rule(rule, host, ip):
            return True, f"allow:{rule}"
    return False, "default-deny"


def append_access_log(repo_dir: Path, entry: dict) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **entry,
    }
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    path = sandbox_access_log_path(repo_dir)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def list_observed_accesses(repo_dir: Path, limit: int = 500) -> list[dict]:
    path = sandbox_access_log_path(repo_dir)
    if not path.exists():
        return []

    grouped: dict[tuple[str, int, str], dict] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []

    for raw in lines:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        host = row.get("host") or row.get("ip") or "unknown"
        port = int(row.get("port") or 0)
        kind = row.get("kind") or "unknown"
        key = (host, port, kind)
        entry = grouped.get(key)
        if entry is None:
            entry = {
                "host": host,
                "port": port,
                "kind": kind,
                "ips": set(),
                "labels": set(),
                "attempts": 0,
                "allowed": 0,
                "blocked": 0,
                "first_seen": row.get("timestamp"),
                "last_seen": row.get("timestamp"),
                "reasons": {},
            }
            grouped[key] = entry
        ip = row.get("ip")
        if ip:
            entry["ips"].add(ip)
        label = row.get("label")
        if label:
            entry["labels"].add(label)
        entry["attempts"] += 1
        if row.get("decision") == "allowed":
            entry["allowed"] += 1
        else:
            entry["blocked"] += 1
        reason = row.get("reason") or "unknown"
        entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1
        ts = row.get("timestamp")
        if ts and (entry["first_seen"] is None or ts < entry["first_seen"]):
            entry["first_seen"] = ts
        if ts and (entry["last_seen"] is None or ts > entry["last_seen"]):
            entry["last_seen"] = ts

    rows = []
    for entry in grouped.values():
        reasons = sorted(
            entry["reasons"].items(),
            key=lambda item: (-item[1], item[0]),
        )
        rows.append({
            "host": entry["host"],
            "port": entry["port"],
            "kind": entry["kind"],
            "ips": sorted(entry["ips"]),
            "labels": sorted(entry["labels"]),
            "attempts": entry["attempts"],
            "allowed": entry["allowed"],
            "blocked": entry["blocked"],
            "first_seen": entry["first_seen"],
            "last_seen": entry["last_seen"],
            "top_reason": reasons[0][0] if reasons else "unknown",
        })
    rows.sort(key=lambda row: (row.get("last_seen") or "", row["attempts"]), reverse=True)
    return rows[:limit]


def sandbox_capabilities() -> dict:
    missing = [name for name in REQUIRED_BINARIES if not shutil.which(name)]
    result = {
        "available": False,
        "missing": missing,
        "details": "",
    }
    if missing:
        result["details"] = f"missing required binaries: {', '.join(missing)}"
        return result
    try:
        # Probe the full layering: rootlesskit (user+net ns) + bwrap (mount ns).
        # Use /bin/true directly so we don't depend on $PATH resolution inside
        # bwrap's minimal mount namespace.
        probe = subprocess.run(
            ["rootlesskit", "--net=none",
             "bwrap", "--ro-bind", "/usr", "/usr",
             "--ro-bind-try", "/bin", "/bin",
             "--ro-bind-try", "/lib", "/lib",
             "--ro-bind-try", "/lib64", "/lib64",
             "--proc", "/proc", "--dev", "/dev",
             "--", "/usr/bin/true"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        result["details"] = str(exc)
        return result
    result["available"] = probe.returncode == 0
    result["details"] = (probe.stderr or probe.stdout or "").strip()
    return result


def _ensure_sandbox_resolv_conf(repo_dir: Path) -> Path:
    """Write a resolv.conf pointing at slirp4netns's built-in DNS (10.0.2.3).

    We bind /etc read-only from the host, which would otherwise shadow
    rootlesskit's resolv.conf override. By binding a freshly-written
    resolv.conf at /etc/resolv.conf, DNS keeps working inside the sandbox.
    """
    path = sandbox_dir(repo_dir) / "resolv.conf"
    # slirp4netns's default DNS forwarder — see slirp4netns(1) --dns-forward.
    path.write_text("nameserver 10.0.2.3\nsearch .\n")
    return path


def build_bwrap_args(repo_dir: Path, config: dict, cwd: Path | str | None = None) -> list[str]:
    """Build bwrap flags from the sandbox config's file rules.

    System paths are always bound read-only. The user's file_rw/file_ro
    rules add to or override them. Anything not bound is invisible to the
    sandboxed process.

    The *cwd* (defaults to ``repo_dir``) is bind-mounted read-write and
    used as the sandbox's working directory, so commands launched from a
    subdirectory or worktree land where the caller expects.

    Runtime essentials always added: /tmp tmpfs, /proc, /dev, /sys (ro),
    and a generated /etc/resolv.conf pointing at slirp4netns's DNS.
    """
    if cwd is None:
        cwd = repo_dir
    cwd = str(Path(cwd).resolve())
    args: list[str] = []

    # System read-only binds — bwrap fails on missing paths, so skip silently.
    for path in _SYSTEM_RO_BINDS:
        if Path(path).exists():
            args.extend(["--ro-bind", path, path])

    # Override /etc/resolv.conf — host's resolv.conf points at 127.0.0.53
    # (systemd-resolved) which is unreachable inside the network namespace.
    resolv_path = _ensure_sandbox_resolv_conf(repo_dir)
    args.extend(["--ro-bind", str(resolv_path), "/etc/resolv.conf"])

    # Default writable: repo (so experiments/agents can write), agent creds.
    default_rw = default_file_rw(repo_dir)
    # The current working directory — whether that's the repo, a subdir, or
    # an experiment worktree under .the_lab/worktrees/ — must be writable or
    # the command fails immediately. Prepend so it wins over defaults.
    default_rw = [cwd, *default_rw]
    # Default read-only: user's local bin, node, gitconfig, etc.
    default_ro = default_file_ro(repo_dir)

    # Merge user rules with defaults; user rules take precedence for the same path.
    user_rw = set(config.get("file_rw") or [])
    user_ro = set(config.get("file_ro") or [])

    rw_paths: list[str] = []
    seen: set[str] = set()
    for path in list(user_rw) + default_rw:
        if path in seen:
            continue
        seen.add(path)
        if Path(path).exists():
            rw_paths.append(path)

    ro_paths: list[str] = []
    for path in list(user_ro) + default_ro:
        if path in seen or path in rw_paths:
            continue  # rw wins over ro for the same path
        seen.add(path)
        if Path(path).exists():
            ro_paths.append(path)

    # Runtime essentials — must come BEFORE user binds so that a
    # subsequent --ro-bind under /tmp isn't shadowed by the tmpfs.
    args.extend([
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
        "--ro-bind-try", "/sys", "/sys",
    ])

    # Ensure HOME exists as a traversable directory so tools that chdir to
    # $HOME or write under it (e.g. `uv` caches) don't hit ENOENT.
    home = os.path.expanduser("~")
    if home and home != "/":
        args.extend(["--dir", home])

    for path in rw_paths:
        args.extend(["--bind", path, path])
    for path in ro_paths:
        args.extend(["--ro-bind", path, path])

    # Chdir to repo_dir so `python -m the_lab.sandbox_guest` can resolve the
    # package. sandbox_guest will chdir into the target *cwd* before exec'ing
    # the user's command.
    args.extend(["--chdir", str(repo_dir)])

    return args


def build_sandbox_command(
    repo_dir: Path,
    kind: str,
    label: str,
    target_cmd: list[str],
    config: dict | None = None,
    cwd: Path | str | None = None,
) -> list[str]:
    """Wrap *target_cmd* in rootlesskit (network namespace) + bwrap (file
    isolation) + sandbox_guest (iptables + proxy + privilege drop).

    The *cwd* (defaults to ``repo_dir``) becomes the sandbox's working
    directory and is bind-mounted read-write so commands invoked from a
    subdirectory or experiment worktree work as expected.

    Layering:
        rootlesskit --net=slirp4netns
          → bwrap [file binds]
            → python -m the_lab.sandbox_guest
              → the target command
    """
    if config is None:
        config = load_sandbox_config(repo_dir)

    bwrap_args = build_bwrap_args(repo_dir, config, cwd=cwd)
    target_cwd = str(Path(cwd).resolve()) if cwd is not None else str(repo_dir)

    guest_cmd = [
        sys.executable,
        "-m",
        "the_lab.sandbox_guest",
        "--repo",
        str(repo_dir),
        "--kind",
        kind,
        "--label",
        label,
        "--cwd",
        target_cwd,
        "--",
        *target_cmd,
    ]

    return [
        "rootlesskit",
        "--net=slirp4netns",
        # No --copy-up=/etc: bwrap provides a clean read-only /etc from the host.
        "bwrap",
        *bwrap_args,
        "--",
        *guest_cmd,
    ]


def save_runtime_info(repo_dir: Path, payload: dict) -> dict:
    _write_json(sandbox_runtime_path(repo_dir), payload)
    return payload


def load_runtime_info(repo_dir: Path) -> dict:
    data = _read_json(sandbox_runtime_path(repo_dir), {})
    return {
        "api_scheme": data.get("api_scheme", "http"),
        "api_port": int(data.get("api_port", 8000) or 8000),
        "api_host": data.get("api_host", "0.0.0.0"),
    }
