"""Network sandbox configuration and launch helpers."""
from __future__ import annotations

import fnmatch
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
    "api.anthropic.com",
    "platform.claude.com",
    "*.claude.ai",
    "api.openai.com",
    "sentry.io",
    "*.sentry.io",
    "statsig.anthropic.com",
    "*.datadoghq.com",
]

REQUIRED_BINARIES = [
    "rootlesskit",
    "slirp4netns",
    "iptables",
    "ip",
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


def default_sandbox_config(repo_dir: Path) -> dict:
    return {
        "enabled": True,
        "mode": "default-deny",
        "allowlist": [],
        "denylist": [],
        "builtin_allowlist": builtin_allowlist(repo_dir),
    }


def load_sandbox_config(repo_dir: Path) -> dict:
    stored = _read_json(sandbox_config_path(repo_dir), {})
    config = default_sandbox_config(repo_dir)
    config["enabled"] = bool(stored.get("enabled", True))
    config["allowlist"] = normalize_rules(stored.get("allowlist", []))
    config["denylist"] = normalize_rules(stored.get("denylist", []))
    return config


def save_sandbox_config(repo_dir: Path, payload: dict) -> dict:
    stored = {
        "enabled": bool(payload.get("enabled", True)),
        "allowlist": normalize_rules(payload.get("allowlist", [])),
        "denylist": normalize_rules(payload.get("denylist", [])),
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
        probe = subprocess.run(
            ["rootlesskit", "--net=none", "true"],
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


def build_sandbox_command(repo_dir: Path, kind: str, label: str, target_cmd: list[str]) -> list[str]:
    cmd = [
        "rootlesskit",
        "--net=slirp4netns",
        "--copy-up=/etc",
    ]
    if kind in ("claude", "codex"):
        # Agent CLIs use /tmp/claude-<uid>/ for their Bash sandbox workspace.
        # --copy-up=/tmp creates a tmpfs overlay so the dropped-privilege
        # process can write there without affecting the host's /tmp.
        cmd.append("--copy-up=/tmp")
    cmd.extend([
        sys.executable,
        "-m",
        "the_lab.sandbox_guest",
        "--repo",
        str(repo_dir),
        "--kind",
        kind,
        "--label",
        label,
        "--",
        *target_cmd,
    ])
    return cmd


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
