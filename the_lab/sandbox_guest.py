"""Guest process that enforces network policy inside a rootless namespace."""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
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


class ExplicitProxy:
    def __init__(self, repo_dir: Path, kind: str, label: str):
        self.repo_dir = repo_dir
        self.kind = kind
        self.label = label

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(reader.read(8192), timeout=2)
        except asyncio.TimeoutError:
            first = b""

        req = _parse_proxy_request(first)
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
                    "ip": None,
                    "port": req["port"],
                    "protocol": req["protocol"],
                    "decision": "blocked",
                    "reason": reason,
                },
            )
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

        try:
            remote_reader, remote_writer = await asyncio.open_connection(req["host"], req["port"])
        except OSError:
            append_access_log(
                self.repo_dir,
                {
                    "kind": self.kind,
                    "label": self.label,
                    "host": req["host"],
                    "ip": None,
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
        ip = peer[0] if peer else None
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

        if req["tunnel"]:
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


def _run_iptables() -> None:
    commands = [
        ["ip", "link", "set", "lo", "up"],
        ["iptables", "-F", "OUTPUT"],
        ["iptables", "-A", "OUTPUT", "-m", "owner", "--uid-owner", "0", "-j", "ACCEPT"],
        ["iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
        ["iptables", "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ["iptables", "-A", "OUTPUT", "-j", "REJECT", "--reject-with", "icmp-port-unreachable"],
    ]
    for cmd in commands:
        subprocess.run(cmd, check=True)


def _drop_privileges() -> None:
    uid = int(os.environ.get("THE_LAB_SANDBOX_TARGET_UID", "1000"))
    gid = int(os.environ.get("THE_LAB_SANDBOX_TARGET_GID", str(uid)))
    try:
        os.setgroups([])
    except OSError:
        pass
    os.setgid(gid)
    os.setuid(uid)


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


async def _run_target(command: list[str], env: dict[str, str]) -> int:
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


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Run a command inside the_lab network sandbox")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--label", required=True)
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
    _run_iptables()

    try:
        return await _run_target(command, _target_env(api_scheme, api_port))
    finally:
        proxy_server.close()
        forward_server.close()
        await proxy_server.wait_closed()
        await forward_server.wait_closed()


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
