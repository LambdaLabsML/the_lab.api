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

from . import jsonio
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

# What each required binary is for, and the package that ships it. Surfaced
# verbatim when the sandbox runtime is unavailable: "missing bwrap" means
# nothing to someone who has never heard of bubblewrap, so every message says
# what the tool does and gives a copy-pasteable install command.
# ``packages`` maps a distro family (see _distro_family) to its package name;
# "default" covers Debian/Ubuntu naming, which most families share.

# GPU device nodes passed through to the sandbox.
#
# ``--dev /dev`` mounts a *fresh* tmpfs and bwrap populates only a minimal node
# set (null/zero/random/tty/pts/...). Without explicit --dev-bind-try, every GPU
# node is invisible inside the sandbox even when the host has them, so CUDA
# fails with things like vLLM's "RuntimeError: Failed to infer device type".
# The sandbox isolates *network and files*, not hardware, so pass these through
# whenever the host has them.
#
# Globs are expanded at build time (one node per GPU: nvidia0, nvidia1, ...).
# Everything is best-effort: --dev-bind-try skips sources that don't exist.
_GPU_DEV_GLOBS = [
    "/dev/nvidiactl",         # NVIDIA control node — required for any CUDA init
    "/dev/nvidia-uvm",        # unified memory — required by CUDA runtime
    "/dev/nvidia-uvm-tools",
    "/dev/nvidia-modeset",
    "/dev/nvidia[0-9]*",      # one per GPU
    "/dev/nvidia-caps",       # MIG capability nodes (directory)
    "/dev/dri",               # DRM render nodes (directory) — non-NVIDIA compute too
    "/dev/kfd",               # AMD ROCm
]


def gpu_device_binds() -> list[str]:
    """bwrap flags passing the host's GPU device nodes into the sandbox.

    Returns an empty list on a host with no GPU nodes (so the sandbox is
    unchanged there). Must be emitted AFTER ``--dev /dev`` or the tmpfs
    shadows these binds.
    """
    import glob as _glob

    args: list[str] = []
    for pattern in _GPU_DEV_GLOBS:
        for path in sorted(_glob.glob(pattern)):
            args.extend(["--dev-bind-try", path, path])
    return args


def gpu_host_state() -> dict:
    """Whether this host can offer GPUs to the sandbox, and if not, why.

    Distinguishes the two situations that look identical from inside a failing
    experiment: no GPU at all, versus a loaded driver whose device nodes were
    never passed into *this* container (the usual `docker run` without
    `--gpus all`). The second is not fixable from inside — hence the hint.
    """
    import glob as _glob

    nodes = sorted({p for pattern in _GPU_DEV_GLOBS for p in _glob.glob(pattern)})
    driver_loaded = Path("/proc/driver/nvidia/version").exists()
    state = {"devices": nodes, "driver_loaded": driver_loaded, "warning": ""}
    if nodes or not driver_loaded:
        return state
    # Driver is loaded but no device nodes exist anywhere in this namespace.
    state["warning"] = (
        "The NVIDIA driver is loaded on this machine, but no GPU device nodes "
        "(/dev/nvidiactl, /dev/nvidia0, /dev/nvidia-uvm) exist in this namespace, "
        "so CUDA will fail inside AND outside the sandbox. This normally means the "
        "container was started without GPU passthrough — restart it with "
        "`--gpus all` (nvidia container runtime), or pass the nodes explicitly: "
        "--device /dev/nvidiactl --device /dev/nvidia0 --device /dev/nvidia-uvm. "
        "It cannot be fixed from inside the container: mknod is denied and "
        "nvidia-modprobe is usually absent."
    )
    return state


_BINARY_INFO: dict[str, dict] = {
    "rootlesskit": {
        "purpose": "runs the sandbox as your own user, without root",
        "packages": {"default": "rootlesskit"},
    },
    "slirp4netns": {
        "purpose": "gives the sandbox its own network stack, so its traffic can be filtered",
        "packages": {"default": "slirp4netns"},
    },
    "iptables": {
        "purpose": "enforces the host allowlist/denylist on that network",
        "packages": {"default": "iptables"},
    },
    "ip": {
        "purpose": "configures the sandbox's virtual network interface",
        "packages": {"default": "iproute2", "fedora": "iproute"},
    },
    "bwrap": {
        "purpose": "the filesystem jail — only paths in your file rules are visible",
        "packages": {"default": "bubblewrap"},
    },
}

# Install command per distro family, keyed by the ID/ID_LIKE in /etc/os-release.
_INSTALL_COMMANDS = {
    "debian": "sudo apt install {pkgs}",
    "fedora": "sudo dnf install {pkgs}",
    "arch":   "sudo pacman -S --needed {pkgs}",
    "suse":   "sudo zypper install {pkgs}",
    "alpine": "sudo apk add {pkgs}",
}


_PROBE_FAILURE_HINT = (
    "This usually means unprivileged user namespaces are disabled or restricted.\n"
    "Common fixes (need root, and may be blocked by your admin or container runtime):\n"
    "  sudo sysctl -w kernel.unprivileged_userns_clone=1   # Debian/Ubuntu\n"
    "  sudo sysctl -w user.max_user_namespaces=15000       # if set to 0\n"
    "  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0   # Ubuntu 24.04+\n"
    "Inside Docker/Kubernetes, nested namespaces often need --privileged or\n"
    "seccomp/AppArmor changes on the outer container.\n"
    "To run without isolation instead, disable the sandbox in the Sandbox tab."
)


def _distro_family() -> str | None:
    """Best-effort distro family from /etc/os-release (ID, then ID_LIKE)."""
    try:
        fields = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                fields[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return None
    candidates = [fields.get("ID", "")] + fields.get("ID_LIKE", "").split()
    for cand in candidates:
        cand = cand.lower()
        if cand in _INSTALL_COMMANDS:
            return cand
        # Map common IDs onto their family.
        if cand in ("ubuntu", "linuxmint", "pop", "raspbian"):
            return "debian"
        if cand in ("rhel", "centos", "rocky", "almalinux"):
            return "fedora"
        if cand in ("manjaro", "endeavouros"):
            return "arch"
        if cand in ("opensuse", "opensuse-leap", "opensuse-tumbleweed", "sles"):
            return "suse"
    return None


def _package_for(binary: str, family: str | None) -> str:
    pkgs = _BINARY_INFO.get(binary, {}).get("packages", {})
    return pkgs.get(family or "", pkgs.get("default", binary))


def _missing_binary_help(missing: list[str]) -> dict:
    """Build the human-facing explanation for missing sandbox binaries."""
    family = _distro_family()
    requirements = [
        {
            "binary": name,
            "package": _package_for(name, family),
            "purpose": _BINARY_INFO.get(name, {}).get("purpose", ""),
        }
        for name in missing
    ]
    # Deduplicate while preserving order — distinct binaries can share a package.
    seen: set[str] = set()
    packages = [r["package"] for r in requirements
                if not (r["package"] in seen or seen.add(r["package"]))]
    install_command = (
        _INSTALL_COMMANDS[family].format(pkgs=" ".join(packages)) if family
        else f"install these packages with your system package manager: {' '.join(packages)}"
    )
    plural = "programs" if len(missing) != 1 else "program"
    summary = (
        f"The sandbox needs {len(missing)} more {plural} on this machine: "
        f"{', '.join(missing)}."
    )
    lines = [
        summary,
        "",
        "The sandbox isolates experiments using standard Linux tooling, so these",
        "must be installed on the host — The Lab cannot ship them:",
        "",
    ]
    width = max(len(r["binary"]) for r in requirements)
    for r in requirements:
        lines.append(f"  {r['binary']:<{width}}  {r['purpose']} (package: {r['package']})")
    lines += [
        "",
        f"Install them with:  {install_command}",
        "",
        # Channel-neutral: this same text is printed by the CLI and returned to
        # the dashboard, which renders its own trailing hint from the structured
        # fields instead.
        "Re-run afterwards to re-check. To run without isolation instead, disable",
        "the sandbox in the dashboard's Sandbox tab.",
    ]
    return {
        "reason": "missing_binaries",
        "requirements": requirements,
        "install_command": install_command,
        "summary": summary,
        "details": "\n".join(lines),
    }


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
    jsonio.write_json(path, data)


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
    home = Path(os.path.expanduser("~"))
    candidates = [
        str(home / ".local"),
        str(home / ".config"),
        str(home / ".gitconfig"),
        str(home / ".gitignore_global"),
    ]
    preamble = repo_dir / ".the_lab" / "preamble.sh"
    if preamble.exists():
        arc_root = preamble.resolve().parent
        candidates.extend([
            str(arc_root / "preamble.sh"),
            str(arc_root / "postamble.sh"),
            str(arc_root / ".eval_config"),
            str(arc_root / "arc3_server"),
            str(arc_root / "viewer"),
        ])
    # Auto-detect nvm node install so Claude Code can spawn node.
    nvm_dir = home / ".nvm" / "versions" / "node"
    if nvm_dir.exists():
        for entry in nvm_dir.iterdir():
            if entry.is_dir():
                candidates.append(str(entry))
    # Ensure the_lab package source is accessible inside the sandbox so that
    # `python -m the_lab.sandbox_guest` works when installed as an editable
    # (pipx/pip install -e) install pointing outside ~/.local.
    try:
        import the_lab as _tl
        pkg_src = str(Path(_tl.__file__).resolve().parent.parent)
        if pkg_src not in candidates:
            candidates.append(pkg_src)
    except Exception:
        pass
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


def _to_relative(path: str, base: str) -> str:
    """Return path relative to base when possible, else return as-is."""
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return path  # outside repo — keep absolute


def load_sandbox_config(repo_dir: Path) -> dict:
    stored = _read_json(sandbox_config_path(repo_dir), {})
    config = default_sandbox_config(repo_dir)
    config["enabled"] = bool(stored.get("enabled", False))
    config["allowlist"] = normalize_rules(stored.get("allowlist", []))
    config["denylist"] = normalize_rules(stored.get("denylist", []))
    # Paths are stored relative to repo_dir; return them as-is so the UI
    # shows relative paths (portable across worktrees).  build_bwrap_args
    # resolves them to absolute when constructing the bwrap command.
    config["file_rw"] = [p for p in stored.get("file_rw", []) if p]
    config["file_ro"] = [p for p in stored.get("file_ro", []) if p]
    config["has_disable_password"] = bool(stored.get("disable_password_hash"))
    return config


def save_sandbox_config(repo_dir: Path, payload: dict) -> dict:
    base = str(repo_dir)
    # Normalize to absolute first (resolves any relative inputs), then convert
    # back to relative so config.json stores portable paths that work in
    # worktrees without editing.
    abs_rw = normalize_paths(payload.get("file_rw", []), base_dir=base)
    abs_ro = normalize_paths(payload.get("file_ro", []), base_dir=base)
    stored = {
        "enabled": bool(payload.get("enabled", True)),
        "allowlist": normalize_rules(payload.get("allowlist", [])),
        "denylist": normalize_rules(payload.get("denylist", [])),
        "file_rw": [_to_relative(p, base) for p in abs_rw],
        "file_ro": [_to_relative(p, base) for p in abs_ro],
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
        "summary": "",
        "reason": "",
        "requirements": [],
        "install_command": "",
        # Which GPU nodes the sandbox will pass through, plus a warning when the
        # driver is loaded but the nodes were never passed into this container.
        "gpu": gpu_host_state(),
    }
    if missing:
        result.update(_missing_binary_help(missing))
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
        result["reason"] = "probe_failed"
        result["summary"] = "The sandbox runtime could not be started."
        result["details"] = f"{result['summary']}\n\n{exc}\n\n{_PROBE_FAILURE_HINT}"
        return result
    result["available"] = probe.returncode == 0
    probe_output = (probe.stderr or probe.stdout or "").strip()
    if result["available"]:
        result["details"] = probe_output
        return result
    # All binaries are present but the layered namespaces would not start —
    # on most hosts that means unprivileged user namespaces are restricted.
    # The raw stderr alone ("bwrap: No permissions to creating new namespace")
    # tells people nothing about what to change.
    result["reason"] = "probe_failed"
    result["summary"] = (
        "All sandbox programs are installed, but this machine would not let them "
        "start an isolated namespace."
    )
    result["details"] = "\n\n".join(
        part for part in (
            result["summary"],
            f"The runtime reported:\n  {probe_output}" if probe_output else "",
            _PROBE_FAILURE_HINT,
        ) if part
    )
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
    the host's GPU device nodes (see gpu_device_binds), and a generated
    /etc/resolv.conf pointing at slirp4netns's DNS.
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
    # Paths from load_sandbox_config are relative to repo_dir — resolve to absolute.
    user_rw = set(normalize_paths(config.get("file_rw") or [], base_dir=str(repo_dir)))
    user_ro = set(normalize_paths(config.get("file_ro") or [], base_dir=str(repo_dir)))

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

    # Worktree mirror: when cwd differs from repo_dir (agent runs in a git
    # worktree), blocked files exist at a *different* absolute path inside the
    # worktree. Without this, an agent can bypass file_ro by editing
    # `cwd/tetris_client.py` instead of `repo_dir/tetris_client.py`.
    # For each user_ro path that lives under repo_dir, compute the matching
    # path under cwd and add it as ro too.
    if cwd != str(repo_dir):
        for path in list(user_ro):
            try:
                rel = Path(path).relative_to(repo_dir)
            except ValueError:
                continue  # not under repo_dir — no worktree equivalent
            wt_path = str(Path(cwd) / rel)
            if wt_path in seen:
                continue  # already covered (e.g. explicitly in rw)
            seen.add(wt_path)
            if Path(wt_path).exists():
                ro_paths.append(wt_path)

    # Runtime essentials — must come BEFORE user binds so that a
    # subsequent --ro-bind under /tmp isn't shadowed by the tmpfs.
    args.extend([
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
        "--ro-bind-try", "/sys", "/sys",
    ])

    # GPU nodes must follow --dev /dev, whose fresh tmpfs would shadow them.
    # No-op on hosts without GPUs.
    args.extend(gpu_device_binds())

    # Ensure HOME exists as a traversable directory so tools that chdir to
    # $HOME or write under it (e.g. `uv` caches) don't hit ENOENT.
    home = os.path.expanduser("~")
    if home and home != "/":
        args.extend(["--dir", home])

    for path in rw_paths:
        args.extend(["--bind", path, path])
    for path in ro_paths:
        args.extend(["--ro-bind", path, path])

    # Protect git hooks from tampering. The repo is bound read-write above, but
    # .git/hooks/ is remounted read-only AFTER so bwrap's later mount wins.
    # This prevents an agent from disabling the pre-commit hook (which blocks
    # staged changes to blocked files) or installing a malicious hook.
    for git_dir in [repo_dir / ".git", Path(cwd) / ".git"]:
        hooks_dir = git_dir / "hooks"
        if hooks_dir.is_dir():
            hooks_str = str(hooks_dir)
            args.extend(["--ro-bind", hooks_str, hooks_str])
        # Worktree .git is a file pointing to the main git dir; also protect
        # the main repo's hooks when cwd is a worktree.
        elif git_dir.is_file():
            try:
                # Read "gitdir: /path/to/.git/worktrees/xxx"
                gitdir_line = git_dir.read_text().strip()
                if gitdir_line.startswith("gitdir:"):
                    wt_gitdir = Path(gitdir_line.split(":", 1)[1].strip())
                    # Main git dir is two levels up from the worktree gitdir
                    main_hooks = wt_gitdir.parent.parent / "hooks"
                    if main_hooks.is_dir():
                        hooks_str = str(main_hooks)
                        if hooks_str not in args:
                            args.extend(["--ro-bind", hooks_str, hooks_str])
            except Exception:
                pass

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
    extra_bwrap_args: list[str] | None = None,
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
    missing = [b for b in ("rootlesskit", "bwrap") if not shutil.which(b)]
    if missing:
        raise RuntimeError(
            f"Sandbox requires {' and '.join(missing)} but "
            + ("it is" if len(missing) == 1 else "they are")
            + " not installed.\n"
            "  Ubuntu/Debian:  sudo apt-get install "
            + ("rootlesskit" if "rootlesskit" in missing else "")
            + (" bubblewrap" if "bwrap" in missing else "").strip()
        )

    if config is None:
        config = load_sandbox_config(repo_dir)

    bwrap_args = build_bwrap_args(repo_dir, config, cwd=cwd)
    if extra_bwrap_args:
        bwrap_args.extend(extra_bwrap_args)
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
