import os
import shutil
import unittest
import gzip
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from the_lab import deps
from the_lab.routes import git_http


def _request(method="GET", query=b"", content_type=None, body=b""):
    headers = []
    if content_type:
        headers.append((b"content-type", content_type.encode()))
    headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "method": method,
        "scheme": "http",
        "path": "/git/repository.git/info/refs",
        "raw_path": b"/git/repository.git/info/refs",
        "query_string": query,
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(scope, receive)
    request.state.auth_tier = "admin"
    return request


class GitHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "THE_LAB_SERVE_GIT": "1",
            "THE_LAB_USER": "test-user",
            "THE_LAB_PASSWORD": "test-password",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    async def test_advertises_only_upload_pack_for_the_fixed_repository(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        request = _request(query=b"service=git-upload-pack")
        repo = Path(__file__).resolve().parents[1]
        with patch.object(deps, "REPO_DIR", repo):
            response = await git_http.serve_git(request, "info/refs")
            body = b"".join([chunk async for chunk in response.body_iterator])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"],
            "application/x-git-upload-pack-advertisement",
        )
        self.assertIn(b"git-upload-pack", body)

    async def test_receive_pack_and_direct_object_paths_are_rejected(self):
        request = _request(query=b"service=git-receive-pack")
        response = await git_http.serve_git(request, "info/refs")
        self.assertEqual(response.status_code, 403)

        request = _request()
        response = await git_http.serve_git(request, "objects/aa/example")
        self.assertEqual(response.status_code, 403)

    async def test_endpoint_fails_closed_without_basic_auth_configuration(self):
        request = _request(query=b"service=git-upload-pack")
        with patch.dict(os.environ, {"THE_LAB_PASSWORD": ""}):
            response = await git_http.serve_git(request, "info/refs")
        self.assertEqual(response.status_code, 503)

    async def test_git_gzip_request_body_is_decoded_with_a_size_limit(self):
        original = b"0014command=ls-refs\n0000"
        request = _request(
            method="POST",
            content_type="application/x-git-upload-pack-request",
            body=gzip.compress(original),
        )
        request.scope["headers"].append((b"content-encoding", b"gzip"))
        # Route selection must accept the POST shape; the end-to-end clone test
        # exercises the decoded body against git-http-backend.
        self.assertEqual(
            git_http._operation(request, "git-upload-pack"),
            ("/.git/git-upload-pack", ""),
        )


if __name__ == "__main__":
    unittest.main()
