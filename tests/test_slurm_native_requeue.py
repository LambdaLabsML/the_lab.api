"""Regression checks for native Slurm requeue ownership."""
from __future__ import annotations

import unittest
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from the_lab.executors.slurm import SlurmExecutor
from the_lab.routes.experiments import _visible_meta
from the_lab.routes import experiments as experiment_routes
from the_lab.deps import _wrap_script


class NativeRequeueWrapperTests(unittest.TestCase):
    def _executor(self):
        return SlurmExecutor("local", {"partition": "lowprio", "remote_base": "/tmp/jobs"})

    def test_detail_meta_exposes_job_id_but_not_run_token(self):
        visible = _visible_meta({"meta": {
            "slurm_job_id": "1831", "slurm_run_token": "secret-bearer-token",
            "slurm_state": "PENDING", "worktree": "/private/worktree",
        }})
        self.assertEqual(visible["slurm_job_id"], "1831")
        self.assertEqual(visible["slurm_state"], "PENDING")
        self.assertNotIn("slurm_run_token", visible)
        self.assertNotIn("worktree", visible)

    def test_external_content_addressed_png_requires_explicit_db_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; db = Path(tmp) / "image-db"
            bucket = db / "bucket_a"; bucket.mkdir(parents=True); root.mkdir()
            image = bucket / "0123456789abcdef.png"; image.write_bytes(b"png")
            (root / "images").symlink_to(db)
            old_root = experiment_routes.REPO_DIR; experiment_routes.REPO_DIR = root
            try:
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("THE_LAB_IMAGE_DB_ROOT", None)
                    with self.assertRaisesRegex(Exception, "access denied"):
                        experiment_routes.serve_repo_file("images/bucket_a/0123456789abcdef.png")
                with patch.dict(os.environ, {"THE_LAB_IMAGE_DB_ROOT": str(db)}):
                    response = experiment_routes.serve_repo_file("images/bucket_a/0123456789abcdef.png")
                self.assertEqual(response.media_type, "image/png")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            finally:
                experiment_routes.REPO_DIR = old_root

    def test_external_image_allowlist_denies_escape_non_png_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; db = Path(tmp) / "image-db"; outside = Path(tmp) / "outside"
            (db / "bucket").mkdir(parents=True); outside.mkdir(); root.mkdir()
            (db / "bucket" / "0123456789abcdef.jpg").write_bytes(b"jpg")
            (outside / "0123456789abcdef.png").write_bytes(b"png")
            (root / "images").symlink_to(db)
            (root / "outside").symlink_to(outside)
            old_root = experiment_routes.REPO_DIR; experiment_routes.REPO_DIR = root
            try:
                with patch.dict(os.environ, {"THE_LAB_IMAGE_DB_ROOT": str(db)}):
                    for path in ("images/bucket/0123456789abcdef.jpg",
                                 "outside/0123456789abcdef.png", "../outside/0123456789abcdef.png"):
                        with self.assertRaisesRegex(Exception, "access denied"):
                            experiment_routes.serve_repo_file(path)
            finally:
                experiment_routes.REPO_DIR = old_root

    def _run_wrapped(self, root: Path, content: str):
        script = root / "wrapped.sh"
        script.write_text(_wrap_script(content))
        return subprocess.run(["bash", str(script)], cwd=root,
                              env={**os.environ, "THE_LAB_TOKEN": "test-token"},
                              capture_output=True, text=True)

    def test_optional_missing_preamble_allows_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_wrapped(Path(tmp), "echo script-ran\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("script-ran", result.stdout)

    def test_present_failing_preamble_is_visible_and_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / ".the_lab").mkdir()
            (root / ".the_lab" / "preamble.sh").write_text("echo preamble-detail >&2\nreturn 23\n")
            result = self._run_wrapped(root, "echo should-not-run\n")
        self.assertEqual(result.returncode, 23)
        self.assertIn("preamble-detail", result.stderr)
        self.assertIn("preamble.sh failed (exit 23)", result.stderr)
        self.assertNotIn("should-not-run", result.stdout)

    def test_preamble_errexit_stops_failure_inside_preamble(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / ".the_lab").mkdir()
            (root / ".the_lab" / "preamble.sh").write_text("set -e\nfalse\necho SHOULD_NOT_RUN\n")
            result = self._run_wrapped(root, "echo should-not-run\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SHOULD_NOT_RUN", result.stdout)
        self.assertNotIn("should-not-run", result.stdout)

    def test_preamble_errexit_applies_to_experiment_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / ".the_lab").mkdir()
            (root / ".the_lab" / "preamble.sh").write_text("set -e\n")
            result = self._run_wrapped(root, "false\necho SHOULD_NOT_RUN\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SHOULD_NOT_RUN", result.stdout)

    def test_preamble_stderr_is_not_suppressed_and_hoisted_exports_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / ".the_lab").mkdir()
            (root / ".the_lab" / "preamble.sh").write_text(
                '[ "$HOISTED_VALUE" = "yes" ] || return 9\necho preamble-stderr >&2\n')
            result = self._run_wrapped(root, "export HOISTED_VALUE=yes\necho script-ran\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("preamble-stderr", result.stderr)
        self.assertIn("script-ran", result.stdout)

    def test_wrapper_leaves_requeue_to_slurm_and_fences_generations(self):
        wrapper = self._executor()._build_wrapper("7.1", "/tmp/jobs/7.1", "/tmp/jobs/7.1/worktree")
        self.assertIn("#SBATCH --requeue", wrapper)
        self.assertIn("SLURM_RESTART_COUNT", wrapper)
        self.assertIn("attempts/${_THE_LAB_PREVIOUS_GENERATION", wrapper)
        self.assertIn(": > \"/tmp/jobs/7.1/script.log\"", wrapper)
        self.assertNotIn("/experiments/7.1/requeue", wrapper)
        self.assertIn("X-The-Lab-Restart-Count", wrapper)
        self.assertNotIn('rm -rf "/tmp/jobs/7.1/worktree"', wrapper)

    def test_accounting_uses_root_not_extern(self):
        executor = self._executor()
        output = "123|PREEMPTED|0:0\n123.batch|COMPLETED|0:0\n123.extern|COMPLETED|0:0\n"
        with patch.object(executor, "_ssh_plain", return_value=CompletedProcess([], 0, output, "")):
            self.assertEqual(executor.accounting_status("123"), {
                "job_id": "123", "state": "PREEMPTED", "exit_code": 0, "signal": 0,
            })

    def test_accounting_does_not_mask_failing_batch_with_completed_root(self):
        executor = self._executor()
        output = "123|COMPLETED|0:0|2\n123.batch|FAILED|9:0|2\n123.extern|COMPLETED|0:0|2\n"
        with patch.object(executor, "_ssh_plain", return_value=CompletedProcess([], 0, output, "")):
            item = executor.accounting_status("123")
        self.assertEqual(item["state"], "FAILED")
        self.assertEqual(item["exit_code"], 9)
        self.assertEqual(item["restart_count"], 2)

    def test_accounting_keeps_preempted_root_despite_batch_143(self):
        executor = self._executor()
        output = "123|PREEMPTED|0:0|1\n123.batch|FAILED|0:143|1\n"
        with patch.object(executor, "_ssh_plain", return_value=CompletedProcess([], 0, output, "")):
            self.assertEqual(executor.accounting_status("123")["state"], "PREEMPTED")

    def test_accounting_keeps_running_root_despite_old_failed_batch(self):
        executor = self._executor()
        output = "123|RUNNING|0:0|1\n123.batch|FAILED|9:0|0\n"
        with patch.object(executor, "_ssh_plain", return_value=CompletedProcess([], 0, output, "")):
            self.assertEqual(executor.accounting_status("123")["state"], "RUNNING")

    def test_accounting_ignores_stale_batch_from_prior_restart(self):
        executor = self._executor()
        output = "123|COMPLETED|0:0|1\n123.batch|FAILED|9:0|0\n"
        with patch.object(executor, "_ssh_plain", return_value=CompletedProcess([], 0, output, "")):
            item = executor.accounting_status("123")
        self.assertEqual(item["state"], "COMPLETED")
        self.assertEqual(item["exit_code"], 0)

    def test_poll_status_raises_for_scheduler_failure(self):
        executor = self._executor()
        with patch.object(executor, "_ssh", return_value=CompletedProcess([], 1, "", "down")):
            with self.assertRaisesRegex(RuntimeError, "squeue failed"):
                executor.poll_status("123")

    def test_accounting_rejects_invalid_job_id(self):
        self.assertIsNone(self._executor().accounting_status("123; scancel all"))

    def test_offline_restart_archives_attempt_and_marker_is_plain_integer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "job"
            worktree = root / "worktree"
            worktree.mkdir(parents=True)
            # Exit through the wrapper's signal path: no pull/done network
            # callback and no 120-second finalization wait.
            (root / "script.sh").write_text("#!/bin/bash\nkill -TERM $PPID\n")
            (root / "script.sh").chmod(0o755)
            wrapper = self._executor()._build_wrapper("7.1", str(root), str(worktree))
            wrapper_path = root / "wrapper.sh"
            wrapper_path.write_text(wrapper)
            wrapper_path.chmod(0o755)
            (root / "experiment.json").write_text('{"workers":16}\n')
            env = {**os.environ, "SLURM_JOB_ID": "123", "SLURM_RESTART_COUNT": "0"}
            # Slurm may invoke the wrapper from a submit cwd, not the job root.
            first = subprocess.run(["bash", str(wrapper_path)], cwd=worktree, env=env, timeout=10)
            self.assertEqual(first.returncode, 143)
            self.assertEqual((root / ".the_lab_generation").read_text(), "0\n")
            for name in ("model.ready.json", "script.progress", "script.metrics.jsonl",
                         "script.output.md", "script.output.html", "script.err", ".heartbeat"):
                (root / name).write_text(name)
            env["SLURM_RESTART_COUNT"] = "1"
            second = subprocess.run(["bash", str(wrapper_path)], cwd=worktree, env=env, timeout=10)
            self.assertEqual(second.returncode, 143)
            archived = root / "attempts" / "0"
            self.assertEqual((root / ".the_lab_generation").read_text(), "1\n")
            self.assertTrue((archived / "script.progress").exists())
            self.assertTrue((archived / "script.output.md").exists())
            self.assertFalse((root / "script.progress").exists())
            self.assertEqual((root / "experiment.json").read_text(), '{"workers":16}\n')

    def test_signal_during_model_warmup_exits_promptly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "job"
            worktree = root / "worktree"
            worktree.mkdir(parents=True)
            key = root / "secret.key"
            key.write_text("key\n")
            # The wrapper blocks in readiness polling after this launcher starts.
            (root / "model_server.sh").write_text("#!/bin/bash\nsleep 1000 &\nwait\n")
            (root / "model_server.sh").chmod(0o755)
            executor = SlurmExecutor("local", {"partition": "lowprio", "remote_base": "/tmp/jobs",
                "model_server": {"kind": "qwen38_flash_sglang", "api_key_file": str(key), "model_inference_root": str(root), "readiness_timeout_s": 999}})
            wrapper = executor._build_wrapper("7.1", str(root), str(worktree))
            path = root / "wrapper.sh"
            path.write_text(wrapper); path.chmod(0o755)
            proc = subprocess.Popen(["bash", str(path)], cwd=root, env={**os.environ,
                "SLURM_JOB_ID": "123", "THE_LAB_ASSIGNED_UNITS": "0"})
            try:
                time.sleep(0.1)  # wrapper has installed traps and entered readiness polling
                proc.send_signal(15)
                self.assertEqual(proc.wait(timeout=5), 143)
            finally:
                if proc.poll() is None:
                    proc.kill()

    def test_stable_staged_pull_resets_only_missing_managed_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, local = Path(tmp) / "remote", Path(tmp) / "local"
            job = base / "7.1"; job.mkdir(parents=True); local.mkdir()
            (job / ".the_lab_generation").write_text("1\n")
            (job / "script.log").write_text("new log")
            (local / "script.output.md").write_text("stale")
            (local / "script.heartbeats").write_text("stale beats")
            (local / "model.ready.json.smoke.json").write_text("stale readiness")
            (local / "experiment.json").write_text("keep")
            executor = SlurmExecutor("local", {"partition": "lowprio", "remote_base": str(base)})
            self.assertEqual(executor.pull_results("7.1", local), 1)
            self.assertEqual((local / "script.log").read_text(), "new log")
            self.assertFalse((local / "script.output.md").exists())
            self.assertFalse((local / "script.heartbeats").exists())
            self.assertFalse((local / "model.ready.json.smoke.json").exists())
            self.assertEqual((local / "experiment.json").read_text(), "keep")

    def test_missing_managed_name_does_not_remove_local_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, local = Path(tmp) / "remote", Path(tmp) / "local"
            job = base / "7.1"; job.mkdir(parents=True); local.mkdir()
            (job / ".the_lab_generation").write_text("1\n")
            managed_directory = local / "script.output.md"
            managed_directory.mkdir()
            (managed_directory / "keep").write_text("x")
            executor = SlurmExecutor("local", {"partition": "lowprio", "remote_base": str(base)})
            executor.pull_results("7.1", local)
            self.assertTrue((managed_directory / "keep").exists())

    def test_changed_generation_rejects_staged_pull_without_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, local = Path(tmp) / "remote", Path(tmp) / "local"
            job = base / "7.1"; job.mkdir(parents=True); local.mkdir()
            (job / ".the_lab_generation").write_text("0\n")
            (job / "script.log").write_text("new")
            (local / "script.log").write_text("old")
            executor = SlurmExecutor("local", {"partition": "lowprio", "remote_base": str(base)})
            with patch.object(executor, "restart_count", side_effect=[0, 1]):
                with self.assertRaisesRegex(RuntimeError, "generation changed"):
                    executor.pull_results("7.1", local)
            self.assertEqual((local / "script.log").read_text(), "old")

    def test_unknown_remote_generation_cannot_publish_marked_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, local = Path(tmp) / "remote", Path(tmp) / "local"
            job = base / "7.1"; job.mkdir(parents=True); local.mkdir()
            (job / ".the_lab_generation").write_text("1\n")
            (job / "script.log").write_text("new")
            (local / "script.log").write_text("old")
            executor = SlurmExecutor("local", {"partition": "lowprio", "remote_base": str(base)})
            with patch.object(executor, "restart_count", side_effect=[None, None]):
                with self.assertRaisesRegex(RuntimeError, "generation changed"):
                    executor.pull_results("7.1", local)
            self.assertEqual((local / "script.log").read_text(), "old")

    def test_failed_transfer_leaves_canonical_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local"; local.mkdir()
            (local / "script.log").write_text("old")
            executor = self._executor()
            with patch.object(executor, "restart_count", return_value=0), \
                 patch("the_lab.executors.slurm.subprocess.run",
                       return_value=CompletedProcess([], 23, "", "broken")):
                with self.assertRaisesRegex(RuntimeError, "rsync pull"):
                    executor.pull_results("7.1", local)
            self.assertEqual((local / "script.log").read_text(), "old")

    def test_missing_strict_preamble_fails_before_sbatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "script.sh"
            script.write_text("source .the_lab/preamble.sh\n")
            executor = self._executor()
            with patch.object(executor, "_ssh") as sbatch, \
                 patch.object(executor, "_ssh_plain", return_value=CompletedProcess([], 0, "", "")), \
                 patch.object(executor, "_stage_experiment_snapshot"):
                with self.assertRaisesRegex(RuntimeError, "runtime staging requires git"):
                    executor.submit("7.1", script, {}, Path(tmp), experiment_record={"meta": {}})
            sbatch.assert_not_called()

    def test_submit_stages_frozen_snapshot_before_sbatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); script = root / "script.sh"; script.write_text("echo ok\n")
            executor = self._executor(); events = []; captured = {}
            record = {"id": 7, "label": "7.1", "meta": {
                "workers": 16, "reasoning": {"effort": "xhigh"},
                "stop": ["quoted ' value", "a\"b"], "slurm_run_token": "not-for-snapshot",
            }}
            def copy(source, destination):
                events.append(destination)
                if destination.endswith("/experiment.json"):
                    captured.update(json.loads(Path(source).read_text()))
            with patch.object(executor, "_scp_to_remote", side_effect=copy), \
                 patch.object(executor, "_ssh_plain", return_value=CompletedProcess([], 0, "", "")), \
                 patch.object(executor, "_ssh", side_effect=lambda cmd, **kw: (events.append(cmd) or CompletedProcess([], 0, "77\n", ""))):
                self.assertEqual(executor.submit("7.1", script, {}, root, experiment_record=record), "77")
            self.assertEqual(captured["meta"]["workers"], 16)
            self.assertEqual(captured["meta"]["reasoning"]["effort"], "xhigh")
            self.assertEqual(captured["meta"]["stop"], ["quoted ' value", 'a"b'])
            self.assertNotIn("slurm_run_token", captured["meta"])
            self.assertLess(events.index("/tmp/jobs/7.1/experiment.json"), events.index("sbatch --parsable /tmp/jobs/7.1/wrapper.sh"))

    def test_malformed_snapshot_prevents_sbatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "script.sh"; script.write_text("echo ok\n")
            executor = self._executor()
            with patch.object(executor, "_ssh") as sbatch, \
                 patch.object(executor, "_ssh_plain", return_value=CompletedProcess([], 0, "", "")):
                with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
                    executor.submit("7.1", script, {}, Path(tmp), experiment_record={"meta": {"bad": {1}}})
            sbatch.assert_not_called()

    def test_snapshot_meta_must_be_object(self):
        executor = self._executor()
        with self.assertRaisesRegex(RuntimeError, "meta as an object"):
            executor._stage_experiment_snapshot("/tmp/jobs/7.1", {"meta": []})

    def test_symlinked_preamble_preserves_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"; runtime = repo / ".the_lab"
            runtime.mkdir(parents=True)
            target = Path(tmp) / "trusted-preamble.sh"; target.write_text("echo ok\n")
            (runtime / "preamble.sh").symlink_to(target)
            script = repo / "run.sh"; script.write_text("source .the_lab/preamble.sh\n")
            executor = self._executor()
            commands = []
            with patch.object(executor, "_ssh_plain", side_effect=lambda cmd, **kwargs: (commands.append(cmd) or CompletedProcess([], 0, "", ""))):
                self.assertTrue(executor._stage_runtime_assets(repo, "/remote/worktree", script))
            link = next(cmd for cmd in commands if "ln -s" in cmd)
            self.assertIn(str(target), link)


if __name__ == "__main__":
    unittest.main()
