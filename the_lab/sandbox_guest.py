"""Guest process that enforces network policy inside a rootless namespace."""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import socket
import struct
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from .sandbox import (
    append_access_log,
    decide_access,
    load_runtime_info,
    load_sandbox_config,
)

PROXY_PORT = 18080
HOST_GATEWAY = "10.0.2.2"
HTTP_METHODS = (b"GET ", b"POST ", b"PUT ", b"PATCH ", b"DELETE ", b"HEAD ", b"OPTIONS ", b"CONNECT ")

_AGENT_KINDS = frozenset({"claude", "codex"})

# Linux: from <linux/netfilter_ipv4.h>
SO_ORIGINAL_DST = 80


# ---------------------------------------------------------------------------
# Helpers — parsing
# ---------------------------------------------------------------------------

def _split_authority(authority: str, default_port: int) -> tuple[str, int]:
    authority = authority.strip()
    if authority.startswith("[") and "]" in authority:
        host, rest = authority[1:].split("]", 1)
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, default_port
    if authority.count(":") == 1:
        host, port = authority.rsplit(":", 1)
        if port.isdigit():
            return host, int(port)
    return authority, default_port


def _host_from_headers(lines: list[str]) -> tuple[str | None, int]:
    for line in lines:
        if line.lower().startswith("host:"):
            return _split_authority(line.split(":", 1)[1].strip(), 80)
    return None, 80


def _parse_proxy_request(data: bytes) -> dict | None:
    if not data or not data.startswith(HTTP_METHODS):
        return None
    header_end = data.find(b"\r\n\r\n")
    if header_end == -1:
        header_end = len(data)
        body = b""
    else:
        body = data[header_end + 4:]
    try:
        head = data[:header_end].decode("latin1", "ignore")
    except UnicodeDecodeError:
        return None
    lines = head.split("\r\n")
    if not lines:
        return None
    parts = lines[0].split(" ", 2)
    if len(parts) < 3:
        return None
    method, target, version = parts
    method = method.upper()
    if method == "CONNECT":
        host, port = _split_authority(target, 443)
        return {
            "host": host,
            "port": port,
            "protocol": "http-connect",
            "tunnel": True,
            "payload": b"",
        }

    parsed = urlparse(target)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        first_line = f"{method} {path} {version}"
    else:
        host, port = _host_from_headers(lines[1:])
        first_line = lines[0]
    if not host:
        return None
    rewritten = (first_line + "\r\n" + "\r\n".join(lines[1:]) + "\r\n\r\n").encode("latin1") + body
    return {
        "host": host,
        "port": port,
        "protocol": "http",
        "tunnel": False,
        "payload": rewritten,
    }


# ---------------------------------------------------------------------------
# Transparent proxy helpers
# ---------------------------------------------------------------------------

def _get_original_dst(writer: asyncio.StreamWriter) -> tuple[str, int] | None:
    """Retrieve the original destination of a NAT-redirected connection."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        data = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
        port = struct.unpack("!H", data[2:4])[0]
        ip = socket.inet_ntoa(data[4:8])
        # If it points at the proxy itself, this was a direct (explicit) connection.
        if ip == "127.0.0.1" and port == PROXY_PORT:
            return None
        return ip, port
    except (OSError, struct.error):
        return None


def _extract_tls_sni(data: bytes) -> str | None:
    """Extract the Server Name Indication hostname from a TLS ClientHello."""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        # record header(5) + handshake header(4) + client version(2) + random(32)
        pos = 5 + 4 + 2 + 32
        if pos + 1 > len(data):
            return None
        session_len = data[pos]
        pos += 1 + session_len
        if pos + 2 > len(data):
            return None
        cipher_len = struct.unpack("!H", data[pos:pos + 2])[0]
        pos += 2 + cipher_len
        if pos + 1 > len(data):
            return None
        comp_len = data[pos]
        pos += 1 + comp_len
        if pos + 2 > len(data):
            return None
        ext_len = struct.unpack("!H", data[pos:pos + 2])[0]
        pos += 2
        ext_end = pos + ext_len
        while pos + 4 <= ext_end:
            ext_type = struct.unpack("!H", data[pos:pos + 2])[0]
            ext_data_len = struct.unpack("!H", data[pos + 2:pos + 4])[0]
            pos += 4
            if ext_type == 0:  # SNI
                if pos + 5 <= len(data):
                    name_len = struct.unpack("!H", data[pos + 3:pos + 5])[0]
                    if pos + 5 + name_len <= len(data):
                        return data[pos + 5:pos + 5 + name_len].decode("ascii", "ignore")
            pos += ext_data_len
    except (IndexError, struct.error):
        pass
    return None


def _extract_http_host(data: bytes) -> str | None:
    """Extract the Host header from a plain HTTP request."""
    try:
        end = data.find(b"\r\n\r\n")
        head = data[:end if end != -1 else len(data)].decode("latin1", "ignore")
        for line in head.split("\r\n")[1:]:
            if line.lower().startswith("host:"):
                host = line.split(":", 1)[1].strip()
                # Strip port if present
                if ":" in host:
                    host = host.rsplit(":", 1)[0]
                return host
    except Exception:
        pass
    return None


def _build_transparent_req(first: bytes, writer: asyncio.StreamWriter) -> dict | None:
    """Build a request dict for a transparently redirected connection."""
    orig = _get_original_dst(writer)
    if orig is None:
        return None
    ip, port = orig
    # Try to recover a hostname for logging and policy checks.
    if port == 443:
        host = _extract_tls_sni(first) or ip
    else:
        host = _extract_http_host(first) or ip
    return {
        "host": host,
        "port": port,
        "protocol": "transparent-tls" if port == 443 else "transparent-tcp",
        "tunnel": False,
        "transparent": True,
        "payload": first,
        # Keep the original IP for connecting — more reliable than re-resolving.
        "_connect_ip": ip,
    }


# ---------------------------------------------------------------------------
# Pipe / relay
# ---------------------------------------------------------------------------

async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Proxy handler
# ---------------------------------------------------------------------------

class ExplicitProxy:
    def __init__(self, repo_dir: Path, kind: str, label: str):
        self.repo_dir = repo_dir
        self.kind = kind
        self.label = label

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
      try:
        await self._handle(reader, writer)
      except Exception as exc:
        pass  # swallow handler errors to keep the proxy alive
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(reader.read(8192), timeout=2)
        except asyncio.TimeoutError:
            first = b""

        req = _parse_proxy_request(first)
        if req is None:
            req = _build_transparent_req(first, writer)
        if req is None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return

        config = load_sandbox_config(self.repo_dir)
        allowed, reason = decide_access(config, req["host"], None)

        if not allowed:
            append_access_log(
                self.repo_dir,
                {
                    "kind": self.kind,
                    "label": self.label,
                    "host": req["host"],
                    "ip": req.get("_connect_ip"),
                    "port": req["port"],
                    "protocol": req["protocol"],
                    "decision": "blocked",
                    "reason": reason,
                },
            )
            if not req.get("transparent"):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                try:
                    await writer.drain()
                except ConnectionResetError:
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return

        # Connect to the remote.  For transparent connections use the original
        # IP directly (avoids a DNS lookup the target already performed).
        connect_host = req.get("_connect_ip") or req["host"]
        try:
            remote_reader, remote_writer = await asyncio.open_connection(connect_host, req["port"])
        except OSError:
            append_access_log(
                self.repo_dir,
                {
                    "kind": self.kind,
                    "label": self.label,
                    "host": req["host"],
                    "ip": req.get("_connect_ip"),
                    "port": req["port"],
                    "protocol": req["protocol"],
                    "decision": "blocked",
                    "reason": "connect-failed",
                },
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return

        peer = remote_writer.get_extra_info("peername")
        ip = peer[0] if peer else req.get("_connect_ip")
        append_access_log(
            self.repo_dir,
            {
                "kind": self.kind,
                "label": self.label,
                "host": req["host"],
                "ip": ip,
                "port": req["port"],
                "protocol": req["protocol"],
                "decision": "allowed",
                "reason": reason,
            },
        )

        if req.get("transparent"):
            # Forward the initial bytes the client already sent.
            if req["payload"]:
                remote_writer.write(req["payload"])
        elif req["tunnel"]:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            remote_writer.write(req["payload"])
        try:
            await writer.drain()
        except ConnectionResetError:
            pass
        await remote_writer.drain()

        await asyncio.gather(
            _pipe(reader, remote_writer),
            _pipe(remote_reader, writer),
        )


# ---------------------------------------------------------------------------
# Port forwarder (API gateway)
# ---------------------------------------------------------------------------

async def _start_forwarder(local_port: int, remote_host: str, remote_port: int) -> asyncio.base_events.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            remote_reader, remote_writer = await asyncio.open_connection(remote_host, remote_port)
        except OSError:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
        await asyncio.gather(
            _pipe(reader, remote_writer),
            _pipe(remote_reader, writer),
        )

    return await asyncio.start_server(handle, "127.0.0.1", local_port)


# ---------------------------------------------------------------------------
# iptables
# ---------------------------------------------------------------------------

def _run_iptables(kind: str) -> None:
    commands: list[list[str]] = [
        ["ip", "link", "set", "lo", "up"],
        ["iptables", "-F", "OUTPUT"],
        ["iptables", "-A", "OUTPUT", "-m", "owner", "--uid-owner", "0", "-j", "ACCEPT"],
        ["iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
        ["iptables", "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
    ]
    if kind in _AGENT_KINDS:
        # Agent CLIs (Claude Code, Codex) don't honour HTTP_PROXY.  Use
        # iptables NAT to transparently redirect all their outbound TCP to the
        # proxy so it still gets logged & policy-checked.  DNS (UDP 53) must
        # be allowed through so the target can resolve hostnames.
        commands.extend([
            ["iptables", "-t", "nat", "-F", "OUTPUT"],
            ["iptables", "-t", "nat", "-A", "OUTPUT", "-p", "tcp",
             "-m", "owner", "!", "--uid-owner", "0",
             "!", "-d", "127.0.0.0/8",
             "-j", "REDIRECT", "--to-port", str(PROXY_PORT)],
            # Allow DNS through — the proxy logs the TCP connections, not DNS.
            ["iptables", "-A", "OUTPUT", "-p", "udp", "--dport", "53", "-j", "ACCEPT"],
        ])
    commands.append(
        ["iptables", "-A", "OUTPUT", "-j", "REJECT", "--reject-with", "icmp-port-unreachable"]
    )
    for cmd in commands:
        subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# User namespace — appear as non-root to userspace while keeping NFS access
# ---------------------------------------------------------------------------

def _enter_userns_as_uid(target_uid: int, target_gid: int) -> None:
    """Create a nested user namespace so this process appears as target_uid/gid.

    Must be called AFTER iptables setup (requires CAP_NET_ADMIN as outer uid 0)
    and BEFORE spawning the agent subprocess.

    After this call:
      • os.getuid() returns target_uid  → Claude Code accepts --dangerously-skip-permissions
      • NFS still uses real host uid    → via rootlesskit's outer uid 0 mapping
      • Network policy is degraded      → agent's outer uid is still 0 (iptables uid-owner 0 = ACCEPT)

    Only needed for agent kinds (where Claude Code's root check fires).  Experiment
    runners keep uid 0 because they don't call --dangerously-skip-permissions.
    """
    import ctypes
    import ctypes.util

    CLONE_NEWUSER = 0x10000000
    libc_name = ctypes.util.find_library("c") or "libc.so.6"
    libc = ctypes.CDLL(libc_name, use_errno=True)

    outer_uid = os.getuid()
    outer_gid = os.getgid()

    ret = libc.unshare(ctypes.c_int(CLONE_NEWUSER))
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno), "unshare(CLONE_NEWUSER)")

    # Map: inner target_uid → outer_uid (1 uid)
    with open("/proc/self/uid_map", "w") as f:
        f.write(f"{target_uid} {outer_uid} 1\n")

    # setgroups must be denied before writing gid_map when unprivileged
    with open("/proc/self/setgroups", "w") as f:
        f.write("deny\n")

    with open("/proc/self/gid_map", "w") as f:
        f.write(f"{target_gid} {outer_gid} 1\n")


# ---------------------------------------------------------------------------
# Privilege drop
# ---------------------------------------------------------------------------

def _drop_privileges() -> None:
    uid = int(os.environ.get("THE_LAB_SANDBOX_TARGET_UID", "1000"))
    gid = int(os.environ.get("THE_LAB_SANDBOX_TARGET_GID", str(uid)))
    try:
        os.setgroups([])
    except OSError:
        pass
    os.setgid(gid)
    os.setuid(uid)


def _prepare_agent_home() -> str | None:
    """Copy the user's Claude/Codex config into a temp home accessible after
    privilege-drop.  Returns the temp home path, or None if not needed."""
    import tempfile

    target_uid = int(os.environ.get("THE_LAB_SANDBOX_TARGET_UID", "1000"))
    target_gid = int(os.environ.get("THE_LAB_SANDBOX_TARGET_GID", str(target_uid)))
    real_home = os.environ.get("HOME", str(Path.home()))

    # Use a fresh temp dir every time — previous runs leave files owned by
    # the namespace-mapped UID that we can't rmtree from the next invocation.
    agent_home = tempfile.mkdtemp(prefix="_lab_agent_home_")

    copy_dirs = [".claude", ".config"]
    copy_files = [".claude.json", ".gitconfig", ".gitignore_global"]

    for d in copy_dirs:
        src = os.path.join(real_home, d)
        dst = os.path.join(agent_home, d)
        if os.path.isdir(src):
            try:
                shutil.copytree(src, dst)
            except Exception:
                pass

    for f in copy_files:
        src = os.path.join(real_home, f)
        dst = os.path.join(agent_home, f)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass

    # chown+chmod everything so the target UID can read/write after privilege-drop.
    for dirpath, dirnames, filenames in os.walk(agent_home):
        os.chown(dirpath, target_uid, target_gid)
        os.chmod(dirpath, 0o755)
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            os.chown(fp, target_uid, target_gid)
            os.chmod(fp, 0o644)

    # Create a writable tmp dir — Claude Code uses /tmp/claude-<uid>/ for its
    # workspace, but the host's /tmp is shared and the mapped UID can't write there.
    agent_tmp = os.path.join(agent_home, "tmp")
    os.makedirs(agent_tmp, exist_ok=True)
    os.chown(agent_tmp, target_uid, target_gid)
    os.chmod(agent_tmp, 0o1777)

    return agent_home


def _target_env(api_scheme: str, api_port: int) -> dict[str, str]:
    proxy = f"http://127.0.0.1:{PROXY_PORT}"
    existing_no_proxy = ",".join(
        value for value in [os.environ.get("NO_PROXY"), os.environ.get("no_proxy")] if value
    )
    no_proxy = "127.0.0.1,localhost"
    if existing_no_proxy:
        no_proxy += "," + existing_no_proxy
    env = dict(os.environ)
    env["THE_LAB_API_BASE"] = f"{api_scheme}://127.0.0.1:{api_port}"
    env["THE_LAB_SANDBOX"] = "1"
    env["HTTP_PROXY"] = proxy
    env["HTTPS_PROXY"] = proxy
    env["ALL_PROXY"] = proxy
    env["http_proxy"] = proxy
    env["https_proxy"] = proxy
    env["all_proxy"] = proxy
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    env["NODE_USE_ENV_PROXY"] = "1"
    return env


# ---------------------------------------------------------------------------
# Target process
# ---------------------------------------------------------------------------

async def _run_target(command: list[str], env: dict[str, str]) -> int:
    # bwrap binds the necessary paths into the namespace, so we don't need
    # to copy binaries or resolve executables out of inaccessible dirs here.
    proc = subprocess.Popen(command, env=env, preexec_fn=_drop_privileges)

    loop = asyncio.get_running_loop()

    def _forward(sig: int) -> None:
        if proc.poll() is None:
            proc.send_signal(sig)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, _forward, sig)
        except NotImplementedError:
            signal.signal(sig, lambda *_args, s=sig: _forward(s))

    try:
        return await asyncio.to_thread(proc.wait)
    finally:
        if proc.poll() is None:
            proc.terminate()
            await asyncio.to_thread(proc.wait)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main() -> int:
    parser = argparse.ArgumentParser(description="Run a command inside the_lab network sandbox")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--cwd", default=None,
                        help="Chdir here before exec'ing the target (defaults to --repo)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("sandbox guest requires a target command")

    repo_dir = Path(args.repo).resolve()
    runtime = load_runtime_info(repo_dir)
    api_port = int(runtime.get("api_port", 8000))
    api_scheme = runtime.get("api_scheme", "http")

    proxy = ExplicitProxy(repo_dir, args.kind, args.label)
    proxy_server = await asyncio.start_server(proxy.handle, "127.0.0.1", PROXY_PORT)
    forward_server = await _start_forwarder(api_port, HOST_GATEWAY, api_port)
    _run_iptables(args.kind)

    target_uid = int(os.environ.get("THE_LAB_SANDBOX_TARGET_UID", "1000"))
    target_gid = int(os.environ.get("THE_LAB_SANDBOX_TARGET_GID", str(target_uid)))

    if args.kind in _AGENT_KINDS:
        # Claude Code refuses --dangerously-skip-permissions when uid=0. We're
        # currently uid 0 inside rootlesskit's user namespace (= real host uid,
        # needed for NFS access). Create a nested user namespace so getuid()
        # returns target_uid — Claude accepts the flag while NFS still works.
        _enter_userns_as_uid(target_uid, target_gid)

    env = _target_env(api_scheme, api_port)
    target_cwd = args.cwd or str(repo_dir)
    if args.kind not in _AGENT_KINDS:
        # Remove .venv symlinks — they point to the shared venv which is
        # read-only after privilege-drop.  Let `uv sync` in the experiment
        # script create a fresh writable .venv.
        for entry in os.listdir(target_cwd):
            p = os.path.join(target_cwd, entry)
            if os.path.islink(p) and entry.startswith(".venv"):
                os.unlink(p)

        # Make all directories world-writable so the dropped-privilege
        # process can create files (e.g. sed -i, uv sync).  Only touches
        # directory inodes — much faster than chowning every file on NFS.
        for dirpath, dirnames, filenames in os.walk(target_cwd):
            try:
                os.chmod(dirpath, 0o777)
            except OSError:
                pass

    # /home/ubuntu is mode 750 and inaccessible after privilege-drop.
    # Tools like uv, pip, npm need writable cache dirs under HOME.
    # Create a minimal writable home for all sandbox kinds.
    import tempfile
    sandbox_home = tempfile.mkdtemp(prefix="_lab_sandbox_home_")
    os.chown(sandbox_home, target_uid, target_gid)
    os.chmod(sandbox_home, 0o755)
    env["HOME"] = sandbox_home
    # Point tool-specific cache dirs at the writable sandbox home so they
    # don't try to write under the inaccessible /home/ubuntu/.cache/.
    env["XDG_CACHE_HOME"] = os.path.join(sandbox_home, ".cache")
    env["HF_HOME"] = os.path.join(sandbox_home, ".cache", "huggingface")
    env["UV_CACHE_DIR"] = os.path.join(sandbox_home, ".cache", "uv")

    if args.kind in _AGENT_KINDS:
        # Agent CLIs refuse to run as root (--dangerously-skip-permissions).
        # Prepare a temp home with the user's config so the dropped-privilege
        # process can access API keys, git config, etc.
        agent_home = _prepare_agent_home()
        if agent_home:
            env["HOME"] = agent_home
            env["TMPDIR"] = os.path.join(agent_home, "tmp")

        # Claude Code hardcodes /tmp/claude-<uid>/ for its Bash sandbox.
        # Instead of chowning the host's /tmp/claude-<uid>/ (which breaks
        # the host user's Claude sessions), point TMPDIR at the agent home
        # so Claude creates its workspace there instead.
        # TMPDIR is already set above to agent_home/tmp.

    # Chdir into the caller's intended working directory before launching the
    # target — bwrap initially chdir'd to repo_dir so python -m could resolve
    # the_lab.sandbox_guest; now that we're past imports, land in the right spot.
    try:
        os.chdir(target_cwd)
    except OSError:
        pass

    try:
        return await _run_target(command, env)
    finally:
        proxy_server.close()
        forward_server.close()
        await proxy_server.wait_closed()
        await forward_server.wait_closed()


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
