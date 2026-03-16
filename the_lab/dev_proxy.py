"""Development proxy: sits in front of the real server, holds requests during reloads.

Architecture:
  [client] → [proxy :8000] → [app :PORT_INTERNAL]

On file change:
  1. Proxy sets "reloading" flag → new requests queue up
  2. Kills and restarts the app subprocess
  3. Waits for health check on internal port
  4. Releases queued requests
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import httpx
from watchfiles import awatch


class DevProxy:
    def __init__(self, repo_dir: str, host: str, port: int, watch_dir: Path):
        self.repo_dir = repo_dir
        self.host = host
        self.port = port  # external port (what clients connect to)
        self.internal_port = port + 1  # internal port (actual app)
        self.watch_dir = watch_dir
        self._process: asyncio.subprocess.Process | None = None
        self._reloading = asyncio.Event()
        self._reloading.set()  # start as "ready"
        self._ready = asyncio.Event()

    async def start_app(self):
        """Start the actual uvicorn app on the internal port."""
        env = {**os.environ, "THE_LAB_REPO": self.repo_dir}
        self._process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "uvicorn", "the_lab.app:app",
            "--host", "127.0.0.1",
            "--port", str(self.internal_port),
            "--log-level", "warning",
            env=env,
        )
        # Wait for it to be ready
        for _ in range(50):  # up to 5 seconds
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{self.internal_port}/api/v1/backlog", timeout=0.5)
                    if resp.status_code == 200:
                        return True
            except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout):
                pass
            await asyncio.sleep(0.1)
        return False

    async def stop_app(self):
        """Stop the app subprocess."""
        if self._process and self._process.returncode is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

    async def reload(self):
        """Reload the app, holding requests during restart."""
        self._reloading.clear()
        print("\033[33m⟳ reloading...\033[0m", flush=True)
        t0 = time.monotonic()
        await self.stop_app()
        ok = await self.start_app()
        elapsed = time.monotonic() - t0
        if ok:
            print(f"\033[32m✓ ready ({elapsed:.1f}s)\033[0m", flush=True)
        else:
            print(f"\033[31m✗ app failed to start ({elapsed:.1f}s)\033[0m", flush=True)
        self._reloading.set()

    async def proxy_request(self, scope, receive, send):
        """Forward a single ASGI request to the internal app."""
        # Wait if reloading
        await self._reloading.wait()

        # Build the request from ASGI scope
        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        query = scope.get("query_string", b"").decode()
        url = f"http://127.0.0.1:{self.internal_port}{path}"
        if query:
            url += f"?{query}"

        # Collect request body
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        # Build headers (skip host)
        headers = {}
        for key, value in scope.get("headers", []):
            name = key.decode()
            if name != "host":
                headers[name] = value.decode()

        # Forward to backend
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.request(
                    method, url, headers=headers, content=body,
                    timeout=httpx.Timeout(connect=5, read=86400, write=5, pool=5),
                )
        except httpx.ConnectError:
            # Backend down — send 503
            await send({"type": "http.response.start", "status": 503, "headers": [
                [b"content-type", b"application/json"],
            ]})
            await send({"type": "http.response.body", "body": b'{"error": "backend reloading"}'})
            return

        # Send response back
        resp_headers = [[k.encode(), v.encode()] for k, v in resp.headers.items()
                        if k.lower() not in ("transfer-encoding",)]
        await send({"type": "http.response.start", "status": resp.status_code, "headers": resp_headers})
        await send({"type": "http.response.body", "body": resp.content})

    def asgi_app(self):
        """Return an ASGI app that proxies to the real server."""
        proxy = self

        async def app(scope, receive, send):
            if scope["type"] == "lifespan":
                # Handle lifespan
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await proxy.stop_app()
                        await send({"type": "lifespan.shutdown.complete"})
                        return
                    else:
                        return
            elif scope["type"] == "http":
                await proxy.proxy_request(scope, receive, send)

        return app

    async def watch_and_reload(self):
        """Watch for file changes and trigger reloads."""
        async for changes in awatch(self.watch_dir):
            # Filter to only .py files
            py_changes = [c for c in changes if c[1].endswith(".py")]
            if py_changes:
                changed = [Path(c[1]).name for c in py_changes]
                print(f"\033[36m  changed: {', '.join(changed)}\033[0m", flush=True)
                await self.reload()

    async def run(self):
        """Start everything."""
        print(f"\033[1mThe Lab — dev mode\033[0m")
        print(f"  proxy:    http://{self.host}:{self.port}")
        print(f"  backend:  http://127.0.0.1:{self.internal_port}")
        print(f"  watching: {self.watch_dir}")
        print()

        ok = await self.start_app()
        if ok:
            print(f"\033[32m✓ ready\033[0m", flush=True)
        else:
            print(f"\033[31m✗ app failed to start\033[0m", flush=True)

        # Run watcher in background
        asyncio.create_task(self.watch_and_reload())
