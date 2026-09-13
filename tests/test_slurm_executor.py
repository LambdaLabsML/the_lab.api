"""Focused, offline tests for Slurm wrapper/runtime safety."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from the_lab.executors.slurm import SlurmExecutor


def _executor() -> SlurmExecutor:
    return SlurmExecutor("local", {"ssh_host": "local", "remote_base": "/tmp/lab-jobs"})


def _fake_curl(tmp_path: Path) -> Path:
    tool = tmp_path / "curl"
    tool.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CURL_LOG\"\nexit 0\n")
    tool.chmod(0o755)
    return tool


class SlurmExecutorTests(unittest.TestCase):
  def test_wrapper_persists_nonzero_status_and_uses_json_callbacks(self) -> None:
   with TemporaryDirectory() as directory:
    tmp_path = Path(directory)
    job, wt = tmp_path / "job", tmp_path / "job" / "worktree"
    wt.mkdir(parents=True)
    (job / "script.sh").write_text("#!/bin/sh\nexit 127\n")
    (job / "script.sh").chmod(0o755)
    wrapper = _executor()._build_wrapper("9.1", str(job), str(wt))
    # Avoid the artifact settle delay; this does not alter generated control flow.
    wrapper = wrapper.replace(f'_wait_stable "{job}" 120', f'_wait_stable "{job}" 0')
    path = job / "wrapper.sh"
    path.write_text(wrapper)
    self.assertEqual(subprocess.run(["bash", "-n", str(path)], timeout=10).returncode, 0)
    log = tmp_path / "curl.log"
    _fake_curl(tmp_path)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CURL_LOG": str(log),
           "THE_LAB_API_URL": "http://offline/api", "THE_LAB_TOKEN": "token", "SLURM_JOB_ID": "123"}
    result = subprocess.run(["bash", str(path)], env=env, capture_output=True, text=True, timeout=20)
    self.assertEqual(result.returncode, 127)
    self.assertEqual(json.loads((job / "exit-status.json").read_text()), {
        "exit_code": 127, "preempted": False, "job_id": "123"
    })
    self.assertIs(json.loads((job / "exit-status.json").read_text())["preempted"], False)
    self.assertIn('-d {"exit_code":127,"job_id":"123"}', log.read_text())


  def test_wrapper_missing_required_preamble_fails_before_script(self) -> None:
   with TemporaryDirectory() as directory:
    tmp_path = Path(directory)
    job, wt = tmp_path / "job", tmp_path / "job" / "worktree"
    wt.mkdir(parents=True)
    wrapper = _executor()._build_wrapper("9.2", str(job), str(wt), requires_preamble=True)
    path = job / "wrapper.sh"
    path.write_text(wrapper)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CURL_LOG": str(tmp_path / "curl.log")}
    _fake_curl(tmp_path)
    result = subprocess.run(["bash", str(path)], env=env, capture_output=True, text=True, timeout=20)
    self.assertEqual(result.returncode, 127)
    self.assertIn("requires .the_lab/preamble.sh", result.stderr)
    self.assertEqual(json.loads((job / "exit-status.json").read_text())["exit_code"], 127)

  def test_missing_callback_keeps_worktree_and_success_status(self) -> None:
   with TemporaryDirectory() as directory:
    tmp_path = Path(directory)
    job, wt = tmp_path / "job", tmp_path / "job" / "worktree"
    wt.mkdir(parents=True)
    (job / "script.sh").write_text("#!/bin/sh\nexit 0\n")
    wrapper = _executor()._build_wrapper("9.3", str(job), str(wt))
    wrapper = wrapper.replace(f'_wait_stable "{job}" 120', f'_wait_stable "{job}" 0')
    path = job / "wrapper.sh"
    path.write_text(wrapper)
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")}
    result = subprocess.run(["bash", str(path)], env=env, capture_output=True, text=True, timeout=20)
    self.assertEqual(result.returncode, 0)
    self.assertTrue(wt.exists())
    status = json.loads((job / "exit-status.json").read_text())
    self.assertEqual(status["exit_code"], 0)
    self.assertIs(status["preempted"], False)


  def test_runtime_staging_only_copies_allowlisted_assets(self) -> None:
   with TemporaryDirectory() as directory:
    tmp_path = Path(directory)
    repo, wt = tmp_path / "repo", tmp_path / "worktree"
    (repo / ".the_lab" / "bin").mkdir(parents=True)
    (repo / ".the_lab" / "preamble.sh").write_text("#!/bin/sh\n")
    (repo / ".the_lab" / "bin" / "helper").write_text("#!/bin/sh\n")
    (repo / ".the_lab" / "secret.key").write_text("do-not-stage")
    script = repo / "script.sh"
    script.write_text("source .the_lab/preamble.sh\n.the_lab/bin/helper\n")
    self.assertTrue(_executor()._stage_runtime_assets(repo, str(wt), script))
    self.assertTrue((wt / ".the_lab" / "preamble.sh").exists())
    self.assertTrue((wt / ".the_lab" / "bin" / "helper").exists())
    self.assertFalse((wt / ".the_lab" / "secret.key").exists())


  def test_runtime_staging_preserves_local_preamble_symlink_target(self) -> None:
   with TemporaryDirectory() as directory:
    tmp_path = Path(directory)
    repo, wt = tmp_path / "repo", tmp_path / "worktree"
    runtime = repo / ".the_lab"
    runtime.mkdir(parents=True)
    target = tmp_path / "trusted-runtime" / "preamble.sh"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\n")
    (runtime / "preamble.sh").symlink_to(target)
    script = repo / "script.sh"
    script.write_text("source .the_lab/preamble.sh\n")
    _executor()._stage_runtime_assets(repo, str(wt), script)
    staged = wt / ".the_lab" / "preamble.sh"
    self.assertTrue(staged.is_symlink())
    self.assertEqual(staged.resolve(), target)


  def test_pull_results_raises_on_rsync_failure(self) -> None:
   with TemporaryDirectory() as directory:
    tmp_path = Path(directory)
    def failed(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 23, "", "connection lost")
    with mock.patch("subprocess.run", failed):
        with self.assertRaisesRegex(RuntimeError, "rsync pull"):
            _executor().pull_results("9.4", tmp_path)

  def test_trusted_output_symlink_materializes_before_remote_cleanup(self) -> None:
   with TemporaryDirectory() as directory:
    root = Path(directory)
    repo, remote, local = root / "repo", root / "jobs", root / "local"
    viewer = repo / ".the_lab" / "runtime" / "viewer.html"
    viewer.parent.mkdir(parents=True)
    viewer.write_text("trusted viewer")
    (repo / ".the_lab" / "preamble.sh").symlink_to(viewer.parent / "preamble.sh")
    (viewer.parent / "preamble.sh").write_text("# runtime")
    source = remote / "9.5"
    source.mkdir(parents=True)
    (source / "script.output.html").symlink_to(viewer)
    (source / "script.output.md").write_text("regular markdown")
    local.mkdir()
    executor = SlurmExecutor("local", {"ssh_host": "local", "remote_base": str(remote)}, repo_dir=repo)
    def fake_rsync(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, "", "")
    with mock.patch("subprocess.run", fake_rsync):
        executor.pull_results("9.5", local)
    shutil.rmtree(source)
    output = local / "script.output.html"
    self.assertFalse(output.is_symlink())
    self.assertEqual(output.read_text(), "trusted viewer")
    self.assertEqual((local / "script.output.md").read_text(), "regular markdown")

  def test_untrusted_output_symlink_is_removed(self) -> None:
   with TemporaryDirectory() as directory:
    root = Path(directory)
    repo, remote, local = root / "repo", root / "jobs", root / "local"
    repo.mkdir(); local.mkdir()
    secret = root / "secret.html"; secret.write_text("no")
    source = remote / "9.6"; source.mkdir(parents=True)
    (source / "script.output.html").symlink_to(secret)
    executor = SlurmExecutor("local", {"ssh_host": "local", "remote_base": str(remote)}, repo_dir=repo)
    def fake_rsync(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, "", "")
    with mock.patch("subprocess.run", fake_rsync):
        with self.assertRaisesRegex(RuntimeError, "untrusted Slurm output"):
            executor.pull_results("9.6", local)
    output = local / "script.output.html"
    self.assertFalse(output.exists())
