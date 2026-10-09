"""Clone-with-overrides, submit-time meta validation, idea commit-pinning,
and the pre-dispatch EXPECTED_COMMIT check (see docs/feedback on cloning
experiments and ~85-key manual copy/strip dance)."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from the_lab import deps, git_ops
from the_lab.routes import experiments, ideas
from the_lab.runner import _expected_commit_conflict
from the_lab.store import Store


class _Runner:
    def wake_scheduler(self):
        pass


def _init_git_repo(path: Path) -> None:
    run = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")


class ExperimentCreationValidationTests(unittest.TestCase):
    """Submit-time validation shared by create/batch-create (Task 1)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.store = Store(self.repo)
        idea = self.store.create_idea("widget", [], "main"); self.store.save_idea(idea)
        self.idea_id = idea["id"]
        for p in (patch.object(experiments, "store", self.store),
                  patch.object(experiments, "REPO_DIR", self.repo),
                  patch.object(experiments, "runner", _Runner()),
                  patch.object(deps, "store", self.store),
                  patch.object(deps, "REPO_DIR", self.repo)):
            p.start(); self.addCleanup(p.stop)
        app = FastAPI(); app.include_router(experiments.router)
        self.client = TestClient(app)

    def test_create_rejects_lab_written_runtime_key_in_meta(self):
        resp = self.client.post(f"/api/v1/ideas/{self.idea_id}/experiments", json={
            "description": "x", "meta": {"lr": 0.01, "scorecard": {"score": 1}},
        })
        self.assertEqual(resp.status_code, 422)
        self.assertIn("scorecard", resp.json()["detail"])

    def test_create_rejects_flattened_top_level_fields(self):
        # idea 1007 bug: hyperparameters meant for `meta` flattened onto the
        # body itself used to be silently dropped (meta ended up empty).
        resp = self.client.post(f"/api/v1/ideas/{self.idea_id}/experiments", json={
            "description": "x", "lr": 0.01, "epochs": 5,
        })
        self.assertEqual(resp.status_code, 422)

    def test_create_accepts_clean_meta(self):
        resp = self.client.post(f"/api/v1/ideas/{self.idea_id}/experiments", json={
            "description": "x", "meta": {"lr": 0.01},
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        label = resp.json()["label"]
        self.assertEqual(self.store.get_experiment(label)["meta"], {"lr": 0.01})

    def test_batch_rejects_lab_written_runtime_key_in_entry_meta(self):
        resp = self.client.post(f"/api/v1/ideas/{self.idea_id}/experiments/batch", json={
            "metas": [{"meta": {"lr": 0.01}}, {"meta": {"slurm_job_id": "123"}}],
        })
        self.assertEqual(resp.status_code, 422)
        self.assertIn("slurm_job_id", resp.json()["detail"])

    def test_batch_rejects_flattened_entry(self):
        # Same "flat meta" bug, batch form: a hyperparameter flattened onto
        # metas[i] itself instead of nested under metas[i].meta.
        resp = self.client.post(f"/api/v1/ideas/{self.idea_id}/experiments/batch", json={
            "metas": [{"lr": 0.01}],
        })
        self.assertEqual(resp.status_code, 422)

    def test_batch_rejects_empty_metas(self):
        resp = self.client.post(f"/api/v1/ideas/{self.idea_id}/experiments/batch", json={"metas": []})
        self.assertEqual(resp.status_code, 422)

    def test_batch_creates_one_experiment_per_entry_with_shared_script(self):
        resp = self.client.post(f"/api/v1/ideas/{self.idea_id}/experiments/batch", json={
            "shared_script_content": "#!/bin/bash\necho hi\n",
            "metas": [{"meta": {"lr": 0.01}}, {"meta": {"lr": 0.03}, "tags": ["sweep"]}],
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["count"], 2)
        labels = [c["label"] for c in body["created"]]
        self.assertEqual(len(set(labels)), 2)
        for label in labels:
            exp = self.store.get_experiment(label)
            self.assertEqual(exp["status"], "queued")
            self.assertTrue((self.repo / exp["script"]).exists())


    def test_batch_without_description_names_the_idea_and_what_differs(self):
        resp = self.client.post(f"/api/v1/ideas/{self.idea_id}/experiments/batch", json={
            "shared_script_content": "#!/bin/bash\necho hi\n",
            "metas": [{"meta": {"SEED": 0, "T": 0.6}}, {"meta": {"SEED": 1, "T": 0.6}},
                      {"meta": {"SEED": 2}, "description": "explicit wins"}],
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        descs = [self.store.get_experiment(c["label"])["description"] for c in resp.json()["created"]]
        idea_title = self.store.get_idea(self.idea_id)["description"].splitlines()[0]
        self.assertTrue(descs[0].startswith(idea_title), descs[0])
        self.assertIn("SEED=0", descs[0]); self.assertIn("SEED=1", descs[1])
        self.assertEqual(descs[2], "explicit wins")
        self.assertNotIn("batch experiment", " ".join(descs))


class CloneExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        _init_git_repo(self.repo)
        self.store = Store(self.repo)
        idea = self.store.create_idea("widget", [], "main"); self.store.save_idea(idea)
        idea["branch"] = "main"  # keep it simple: no idea/<id> branch needed for these tests
        self.store.save_idea(idea)
        self.idea_id = idea["id"]
        self.source = self.store.create_experiment(
            self.idea_id, "parent run",
            meta={
                "lr": 0.01, "epochs": 5,
                # Lab-written runtime keys a real run would have accumulated —
                # must never survive into a clone.
                "slurm_job_id": "999", "slurm_state": "RUNNING", "slurm_restart_count": 2,
                "card_id": "abc-123", "competition_mode": True,
                "scorecard": {"score": 0.42}, "git_commit": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "git_branch": "main", "assigned_resource": "gpu-pool", "assigned_units": [1, 2],
                "api_key": "super-secret-value",
            },
            tags=["baseline"],
        )
        script_path = self.repo / self.source["script"]
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text("#!/bin/bash\necho parent\n")
        self.source = self.store.get_experiment(self.source["label"])
        for p in (patch.object(experiments, "store", self.store),
                  patch.object(experiments, "REPO_DIR", self.repo),
                  patch.object(experiments, "runner", _Runner()),
                  patch.object(deps, "store", self.store),
                  patch.object(deps, "REPO_DIR", self.repo)):
            p.start(); self.addCleanup(p.stop)
        app = FastAPI(); app.include_router(experiments.router)
        self.client = TestClient(app)

    def test_clone_strips_lab_written_keys_and_applies_overrides(self):
        resp = self.client.post("/api/v1/experiments/clone", json={
            "clone_of": self.source["label"],
            "overrides": {"lr": 0.02, "api_key": "new-secret-value"},
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        new_label = body["created"][0]["label"]
        new_exp = self.store.get_experiment(new_label)
        self.assertNotEqual(new_label, self.source["label"])
        # Script copied verbatim.
        self.assertEqual((self.repo / new_exp["script"]).read_text(), "#!/bin/bash\necho parent\n")
        # Overridden value applied.
        self.assertEqual(new_exp["meta"]["lr"], 0.02)
        self.assertEqual(new_exp["meta"]["epochs"], 5)
        # Every lab-written runtime key is gone — the clone must not show the
        # PARENT's running state/scores.
        for key in ("slurm_job_id", "slurm_state", "slurm_restart_count", "card_id",
                    "competition_mode", "scorecard", "git_commit", "git_branch",
                    "assigned_resource", "assigned_units"):
            self.assertNotIn(key, new_exp["meta"], key)
        self.assertEqual(new_exp["status"], "queued")
        self.assertEqual(new_exp["tags"], ["baseline"])
        # The diff reports the stripped keys as removed, and masks the secret.
        diff = body["diff"]
        self.assertEqual(diff["changed"]["lr"], {"from": 0.01, "to": 0.02})
        self.assertEqual(diff["removed"]["scorecard"], {"score": 0.42})
        # api_key changed (overridden) — masked in BOTH directions, never
        # echoing either the old or the new secret value.
        self.assertEqual(diff["changed"]["api_key"], {"from": "[REDACTED]", "to": "[REDACTED]"})
        self.assertEqual(new_exp["meta"]["api_key"], "new-secret-value")

    def test_clone_default_description_says_what_changed_and_where_from(self):
        resp = self.client.post("/api/v1/experiments/clone", json={
            "clone_of": self.source["label"], "overrides": {"lr": 0.02, "api_key": "x"}, "seed": 3,
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        new_exp = self.store.get_experiment(resp.json()["created"][0]["label"])
        desc = new_exp["description"]
        self.assertIn("lr=0.02", desc); self.assertIn("seed=3", desc)
        self.assertIn(f"from {self.source['label']}: parent run", desc)
        self.assertNotIn("x", desc.split("api_key=")[1][:3] if "api_key=" in desc else "")  # secret masked
        self.assertEqual(new_exp["meta"]["cloned_from"], self.source["label"])
        plain = self.client.post("/api/v1/experiments/clone", json={"clone_of": self.source["label"]})
        self.assertEqual(self.store.get_experiment(plain.json()["created"][0]["label"])["description"],
                         f"repeat of {self.source['label']}: parent run")

    def test_clone_rejects_lab_written_key_in_overrides(self):
        resp = self.client.post("/api/v1/experiments/clone", json={
            "clone_of": self.source["label"],
            "overrides": {"slurm_state": "RUNNING"},
        })
        self.assertEqual(resp.status_code, 422)
        self.assertIn("slurm_state", resp.json()["detail"])

    def test_clone_count_and_seed_increment_per_clone(self):
        resp = self.client.post("/api/v1/experiments/clone", json={
            "clone_of": self.source["label"], "seed": 10, "count": 3,
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["count"], 3)
        seeds = sorted(self.store.get_experiment(c["label"])["meta"]["seed"] for c in body["created"])
        self.assertEqual(seeds, [10, 11, 12])

    def test_clone_with_commit_pins_trusted_git_context(self):
        full_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                  check=True, capture_output=True, text=True).stdout.strip()
        resp = self.client.post("/api/v1/experiments/clone", json={
            "clone_of": self.source["label"], "commit": full_sha,
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        new_label = resp.json()["created"][0]["label"]
        new_exp = self.store.get_experiment(new_label)
        # trusted_meta=True is required for git_commit/git_branch to survive
        # store.create_experiment's normal stripping of server-owned keys.
        self.assertEqual(new_exp["meta"]["git_commit"], full_sha)
        self.assertEqual(new_exp["meta"]["git_branch"], "main")

    def test_clone_rejects_unresolvable_commit(self):
        resp = self.client.post("/api/v1/experiments/clone", json={
            "clone_of": self.source["label"], "commit": "not-a-real-commit",
        })
        self.assertEqual(resp.status_code, 400)

    def test_clone_of_missing_source_is_404(self):
        resp = self.client.post("/api/v1/experiments/clone", json={"clone_of": "999.1"})
        self.assertEqual(resp.status_code, 404)


class CreateIdeaCommitPinningTests(unittest.TestCase):
    """create_idea(commit=...) points the branch without any checkout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        _init_git_repo(self.repo)
        # A second commit on main so HEAD != the first commit — lets us pin
        # to an OLDER commit and prove the branch does NOT follow HEAD.
        (self.repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "second"],
                       cwd=self.repo, check=True)
        self.old_commit = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=self.repo,
                                         check=True, capture_output=True, text=True).stdout.strip()
        self.store = Store(self.repo)
        for p in (patch.object(ideas, "store", self.store), patch.object(ideas, "REPO_DIR", self.repo),
                  patch.object(deps, "store", self.store), patch.object(deps, "REPO_DIR", self.repo)):
            p.start(); self.addCleanup(p.stop)
        app = FastAPI(); app.include_router(ideas.router)
        self.client = TestClient(app)

    def test_commit_pins_branch_without_checkout_or_carry_over_commit(self):
        before_head = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=self.repo,
                                     check=True, capture_output=True, text=True).stdout.strip()
        resp = self.client.post("/api/v1/ideas/new", json={
            "description": "pinned idea", "parent_ids": [], "commit": self.old_commit,
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertFalse(body["checked_out"])
        branch_commit = subprocess.run(["git", "rev-parse", body["branch"]], cwd=self.repo,
                                       check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(branch_commit, self.old_commit)
        # The caller's checkout never moved off whatever branch it was on.
        after_head = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=self.repo,
                                    check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(before_head, after_head)

    def test_commit_rejects_unresolvable_sha(self):
        resp = self.client.post("/api/v1/ideas/new", json={
            "description": "bad", "parent_ids": [], "commit": "0" * 40,
        })
        self.assertEqual(resp.status_code, 400)

    def test_commit_rejects_multiple_parents(self):
        first = self.client.post("/api/v1/ideas/new", json={"description": "p1", "parent_ids": []})
        second = self.client.post("/api/v1/ideas/new", json={"description": "p2", "parent_ids": []})
        parents = [first.json()["id"], second.json()["id"]]
        resp = self.client.post("/api/v1/ideas/new", json={
            "description": "merge-pinned", "parent_ids": parents, "commit": self.old_commit,
        })
        self.assertEqual(resp.status_code, 400)


class ResolveCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        _init_git_repo(self.repo)

    def test_resolves_head_to_full_sha(self):
        full = git_ops.resolve_commit("HEAD", cwd=self.repo)
        self.assertRegex(full, r"^[0-9a-f]{40}$")

    def test_rejects_unknown_ref(self):
        with self.assertRaises(git_ops.GitError):
            git_ops.resolve_commit("not-a-ref", cwd=self.repo)


class ExpectedCommitPreDispatchTests(unittest.TestCase):
    """Pure-function unit tests for the pre-dispatch commit check (fail
    before the GPU starts, not ~7 minutes in via the in-job script guard)."""

    def test_no_conflict_when_commits_match(self):
        self.assertIsNone(_expected_commit_conflict({"EXPECTED_COMMIT": "abc"}, "abc"))

    def test_no_conflict_when_expected_commit_unset(self):
        self.assertIsNone(_expected_commit_conflict({}, "abc"))

    def test_conflict_when_commits_differ(self):
        msg = _expected_commit_conflict({"EXPECTED_COMMIT": "abc"}, "def")
        self.assertIsNotNone(msg)
        self.assertIn("abc", msg)
        self.assertIn("def", msg)

    def test_no_conflict_when_git_commit_not_yet_resolved(self):
        self.assertIsNone(_expected_commit_conflict({"EXPECTED_COMMIT": "abc"}, None))


if __name__ == "__main__":
    unittest.main()
