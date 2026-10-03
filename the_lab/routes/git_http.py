"""Opt-in, authenticated, read-only Git smart-HTTP transport."""
from __future__ import annotations

import asyncio
import os
import zlib
from pathlib import Path

from fastapi import APIRouter, Request
from starlette.responses import PlainTextResponse, StreamingResponse

from .. import deps

router = APIRouter()

_CLONE_PATH = "/git/repository.git"
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_CGI_HEADERS = 64 * 1024
_FORWARDED_HEADERS = {"cache-control", "content-type", "expires", "pragma"}


def _enabled() -> bool:
    return os.environ.get("THE_LAB_SERVE_GIT", "").strip() == "1"


def _auth_configured() -> bool:
    return bool(
        os.environ.get("THE_LAB_USER", "").strip()
        and os.environ.get("THE_LAB_PASSWORD", "").strip()
    )


def _operation(request: Request, git_path: str) -> tuple[str, str] | None:
    """Map the tiny public route set to a fixed path inside this repo's .git."""
    if request.method == "GET" and git_path == "info/refs":
        if request.query_params.get("service") != "git-upload-pack":
            return None
        if set(request.query_params) != {"service"}:
            return None
        return "/.git/info/refs", "service=git-upload-pack"
    if request.method == "POST" and git_path == "git-upload-pack":
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "application/x-git-upload-pack-request":
            return None
        if request.url.query:
            return None
        return "/.git/git-upload-pack", ""
    return None


def _parse_cgi_headers(raw: bytes) -> tuple[int, dict[str, str]]:
    status = 200
    headers: dict[str, str] = {}
    for line in raw.decode("latin-1").splitlines():
        name, separator, value = line.partition(":")
        if not separator:
            continue
        name = name.strip()
        value = value.strip()
        if name.lower() == "status":
            try:
                status = int(value.split(None, 1)[0])
            except (ValueError, IndexError):
                status = 500
        elif name.lower() in _FORWARDED_HEADERS:
            headers[name] = value
    return status, headers


async def _read_cgi_headers(stream: asyncio.StreamReader) -> tuple[bytes, bytes]:
    buffered = bytearray()
    while len(buffered) <= _MAX_CGI_HEADERS:
        chunk = await stream.read(4096)
        if not chunk:
            break
        buffered.extend(chunk)
        marker = buffered.find(b"\r\n\r\n")
        marker_size = 4
        if marker < 0:
            marker = buffered.find(b"\n\n")
            marker_size = 2
        if marker >= 0:
            return bytes(buffered[:marker]), bytes(buffered[marker + marker_size:])
    raise RuntimeError("invalid or oversized response from git http-backend")


@router.api_route(
    f"{_CLONE_PATH}/{{git_path:path}}",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def serve_git(request: Request, git_path: str):
    """Serve clone/fetch only; never expose receive-pack or arbitrary files."""
    if not _enabled():
        return PlainTextResponse("Not found", status_code=404)
    if not _auth_configured():
        return PlainTextResponse(
            "Git serving requires THE_LAB_USER and THE_LAB_PASSWORD",
            status_code=503,
        )
    if getattr(request.state, "auth_tier", None) != "admin":
        return PlainTextResponse("Admin authentication required", status_code=403)

    selected = _operation(request, git_path)
    if selected is None:
        return PlainTextResponse("Read-only Git endpoint", status_code=403)
    path_info, query_string = selected

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_REQUEST_BYTES:
                return PlainTextResponse("Git request too large", status_code=413)
        except ValueError:
            return PlainTextResponse("Invalid Content-Length", status_code=400)
    body = await request.body()
    if len(body) > _MAX_REQUEST_BYTES:
        return PlainTextResponse("Git request too large", status_code=413)
    content_encoding = request.headers.get("content-encoding", "").strip().lower()
    if content_encoding == "gzip":
        try:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = decoder.decompress(body, _MAX_REQUEST_BYTES + 1)
            if decoder.unconsumed_tail or len(decoded) > _MAX_REQUEST_BYTES:
                return PlainTextResponse("Git request too large", status_code=413)
            decoded += decoder.flush(_MAX_REQUEST_BYTES + 1 - len(decoded))
        except zlib.error:
            return PlainTextResponse("Invalid gzip request body", status_code=400)
        if not decoder.eof or decoder.unused_data:
            return PlainTextResponse("Invalid gzip request body", status_code=400)
        if len(decoded) > _MAX_REQUEST_BYTES:
            return PlainTextResponse("Git request too large", status_code=413)
        body = decoded
    elif content_encoding not in ("", "identity"):
        return PlainTextResponse("Unsupported Content-Encoding", status_code=415)

    repo_dir = Path(deps.REPO_DIR).resolve()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_HTTP_EXPORT_ALL": "1",
        "GIT_PROJECT_ROOT": str(repo_dir),
        "GATEWAY_INTERFACE": "CGI/1.1",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REQUEST_METHOD": request.method,
        "PATH_INFO": path_info,
        "QUERY_STRING": query_string,
        "CONTENT_TYPE": request.headers.get("content-type", ""),
        "CONTENT_LENGTH": str(len(body)),
        "REMOTE_ADDR": request.client.host if request.client else "",
    }
    if request.headers.get("git-protocol") == "version=2":
        env["HTTP_GIT_PROTOCOL"] = "version=2"

    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c", "http.getanyfile=false",
            "-c", "http.receivepack=false",
            "http-backend",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(body)
        await process.stdin.drain()
        process.stdin.close()
        cgi_headers, initial_body = await _read_cgi_headers(process.stdout)
        status, headers = _parse_cgi_headers(cgi_headers)
    except (OSError, RuntimeError) as exc:
        if "process" in locals() and process.returncode is None:
            process.kill()
            await process.wait()
        return PlainTextResponse(f"Git backend unavailable: {exc}", status_code=502)

    async def response_body():
        try:
            if initial_body:
                yield initial_body
            while True:
                chunk = await process.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
            await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    return StreamingResponse(response_body(), status_code=status, headers=headers)
