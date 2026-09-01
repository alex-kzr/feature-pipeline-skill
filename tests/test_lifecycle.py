"""Durable run lifecycle: fresh initialization, atomic flush, resume reconciliation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline_core.lifecycle import CORE_VERSION, RunLifecycle
from pipeline_core.state import ACTOR_EXECUTOR, ACTOR_RUNNER, ResumeError, Run


def _run(root: Path, *, feature: str = "durable", plan: str | None = None) -> Run:
    prompt = root / "prompts" / "feature.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("feature", encoding="utf-8")
    plan_path = None
    if plan is not None:
        plan_path = root / plan
        plan_path.write_text("plan", encoding="utf-8")
    return Run.create(feature, prompt, plan_path, root / "storage" / feature, root)


def _stored(run_dir: Path) -> dict:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


class FreshInitializationTests(unittest.TestCase):
    def test_initialize_persists_schema_v2_state_before_any_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            life = RunLifecycle.initialize(
                run,
                tasks=[("A-1", []), ("A-2", ["A-1"])],
                controls={"adapter": ("claude", "explicit"),
                          "max_repair_attempts": (2, "default")},
            )
            stored = _stored(run.run_dir)
            self.assertEqual(stored["schema_version"], 2)
            self.assertEqual([t["id"] for t in stored["tasks"]], ["A-1", "A-2"])
            self.assertEqual(stored["controls"]["adapter"],
                             {"value": "claude", "sourced": "explicit"})
            self.assertEqual(stored["controls"]["max_repair_attempts"],
                             {"value": 2, "sourced": "default"})
            self.assertIs(life.run, run)

    def test_initialize_records_a_portable_secret_free_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            RunLifecycle.initialize(run, tasks=[("A-1", [])],
                                    adapter_requested="auto", adapter_resolved="claude")
            env = _stored(run.run_dir)["environment"]
            self.assertEqual(env["platform"], sys.platform)
            self.assertEqual(env["python"], "%d.%d.%d" % sys.version_info[:3])
            self.assertEqual(env["core_version"], CORE_VERSION)
            self.assertEqual(env["adapter"], {"requested": "auto", "resolved": "claude"})
            self.assertNotIn("secret", json.dumps(env).lower())

    def test_initialize_rederives_readiness_for_dependency_free_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            RunLifecycle.initialize(run, tasks=[("A-1", []), ("A-2", ["A-1"])])
            tasks = {t["id"]: t["status"] for t in _stored(run.run_dir)["tasks"]}
            self.assertEqual(tasks, {"A-1": "ready", "A-2": "pending"})


class AtomicFlushTests(unittest.TestCase):
    def _life(self, root: Path) -> RunLifecycle:
        return RunLifecycle.initialize(_run(root), tasks=[("A-1", [])])

    def test_transition_flushes_and_leaves_a_history_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = self._life(root)
            life.transition("A-1", "running", actor=ACTOR_RUNNER)
            stored = _stored(life.run.run_dir)
            self.assertEqual(
                [t["status"] for t in stored["tasks"] if t["id"] == "A-1"], ["running"])
            self.assertEqual(stored["history"][-1]["to"], "running")
            self.assertEqual(stored["history"][-1]["scope"], "task:A-1")

    def test_record_command_flushes_and_leaves_a_source_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = self._life(root)
            record = life.record_command("verify", ".", ["python", "-c", "pass"],
                                         0, 0.01, "ok", "")
            stored = _stored(life.run.run_dir)
            self.assertEqual(stored["commands"][-1]["id"], record["id"])
            self.assertEqual(stored["history"][-1]["scope"], f"command:{record['id']}")

    def test_consume_launch_generation_flushes_and_is_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = self._life(root)
            self.assertEqual(life.consume_launch_generation("A-1", "executor"), 1)
            reloaded = RunLifecycle.load(life.run.run_dir, root)
            self.assertEqual(reloaded.consume_launch_generation("A-1", "executor"), 2)
            self.assertEqual(_stored(life.run.run_dir)["history"][-1]["scope"],
                             "generation:A-1:executor")


class ResumeReconciliationTests(unittest.TestCase):
    def _interrupted(self, root: Path, status: str) -> Run:
        run = _run(root, plan="plan.md")
        life = RunLifecycle.initialize(run, tasks=[("A-1", []), ("A-2", ["A-1"])])
        life.transition("A-1", "running", actor=ACTOR_RUNNER)
        if status in {"implemented", "verified", "verification_failed", "repairing"}:
            life.transition("A-1", "implemented", actor=ACTOR_EXECUTOR)
        if status == "verified":
            life.transition("A-1", "verified", actor=ACTOR_RUNNER)
        if status in {"verification_failed", "repairing"}:
            life.transition("A-1", "verification_failed", actor=ACTOR_RUNNER)
        if status == "repairing":
            life.transition("A-1", "repairing", actor=ACTOR_RUNNER)
        life.run.current_task = "A-1"
        life.run.save()
        return run

    def _resume(self, root: Path, run: Run) -> RunLifecycle:
        return RunLifecycle.resume(
            run.run_dir, root, feature="durable",
            prompt_path=root / "prompts" / "feature.md", plan_path=root / "plan.md")

    def test_running_rolls_back_to_ready_and_clears_the_in_flight_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._interrupted(root, "running")
            life = self._resume(root, run)
            self.assertEqual(life.run.task("A-1").status, "ready")
            self.assertIsNone(life.run.current_task)

    def test_repairing_rolls_back_to_verification_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._interrupted(root, "repairing")
            life = self._resume(root, run)
            self.assertEqual(life.run.task("A-1").status, "verification_failed")

    def test_implemented_and_verified_are_preserved_on_resume(self) -> None:
        for status in ("implemented", "verified"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = self._interrupted(root, status)
                life = self._resume(root, run)
                self.assertEqual(life.run.task("A-1").status, status)

    def test_resume_rejects_a_changed_feature_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._interrupted(root, "running")
            with self.assertRaises(ResumeError) as caught:
                RunLifecycle.resume(
                    run.run_dir, root, feature="other",
                    prompt_path=root / "prompts" / "feature.md",
                    plan_path=root / "plan.md")
            self.assertEqual(caught.exception.code, "resume-mismatch")

    def test_resume_rejects_a_removed_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._interrupted(root, "running")
            with self.assertRaises(ResumeError) as caught:
                RunLifecycle.resume(
                    run.run_dir, root, feature="durable",
                    prompt_path=root / "prompts" / "feature.md",
                    plan_path=root / "plan.md",
                    expected_tasks={"A-1": []})
            self.assertEqual(caught.exception.code, "task-set-mismatch")

    def test_resume_rejects_a_changed_dependency_edge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._interrupted(root, "running")
            with self.assertRaises(ResumeError) as caught:
                RunLifecycle.resume(
                    run.run_dir, root, feature="durable",
                    prompt_path=root / "prompts" / "feature.md",
                    plan_path=root / "plan.md",
                    expected_tasks={"A-1": [], "A-2": []})
            self.assertEqual(caught.exception.code, "task-set-mismatch")


class ReadinessAndSuppressionTests(unittest.TestCase):
    def test_readiness_is_recomputed_from_persisted_dependency_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            life = RunLifecycle.initialize(run, tasks=[("A-1", []), ("A-2", ["A-1"])])
            life.transition("A-1", "running", actor=ACTOR_RUNNER)
            life.transition("A-1", "implemented", actor=ACTOR_EXECUTOR)
            life.transition("A-1", "verified", actor=ACTOR_RUNNER)
            # A-2 is still pending on disk; a fresh resume must promote it from the graph.
            reloaded = RunLifecycle.load(run.run_dir, root)
            reloaded.recompute_readiness()
            self.assertEqual(reloaded.run.task("A-2").status, "ready")

    def test_a_stale_ready_flag_is_not_trusted_when_a_dependency_regressed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            life = RunLifecycle.initialize(run, tasks=[("A-1", []), ("A-2", ["A-1"])])
            # Force an inconsistent snapshot: A-2 marked ready while A-1 never verified.
            life.run.task("A-2").status = "ready"
            life.recompute_readiness()
            self.assertEqual(life.run.task("A-2").status, "pending")

    def test_blocked_task_suppresses_transitive_dependents_but_keeps_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            life = RunLifecycle.initialize(
                run, tasks=[("A-1", []), ("A-2", ["A-1"]), ("A-3", ["A-2"])])
            life.block("A-1", "max-repair-attempts-exhausted")
            self.assertEqual(life.run.task("A-1").status, "blocked")
            self.assertEqual(life.run.task("A-2").blocker, "blocked_by: A-1")
            self.assertEqual(life.run.task("A-3").blocker, "blocked_by: A-1")
            # dependents remain in state and are reportable
            self.assertIn("A-3", life.run.tasks)
            self.assertNotIn("A-2", life.eligible_tasks())

    def test_an_attested_dependency_satisfies_readiness_without_being_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            life = RunLifecycle.initialize(run, tasks=[("A-1", []), ("A-2", ["A-1"])])
            life.run.record_attestation(
                "A-2", "A-1",
                {"dep_id": "A-1", "source_feature": "elsewhere", "task_verdict": "PASS"})
            life.recompute_readiness()
            self.assertEqual(life.run.task("A-2").status, "ready")
            # A-1 itself is untouched by the attestation on its dependent.
            self.assertEqual(life.run.task("A-1").status, "ready")

    def test_an_attested_dependency_never_satisfies_an_unattested_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            life = RunLifecycle.initialize(
                run, tasks=[("A-1", []), ("A-2", []), ("A-3", ["A-1", "A-2"])])
            life.run.record_attestation(
                "A-3", "A-1",
                {"dep_id": "A-1", "source_feature": "elsewhere", "task_verdict": "PASS"})
            life.recompute_readiness()
            # A-2 was never attested and never verified: A-3 must stay pending.
            self.assertEqual(life.run.task("A-3").status, "pending")

    def test_eligible_tasks_are_ready_and_unblocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            life = RunLifecycle.initialize(run, tasks=[("A-1", []), ("A-2", [])])
            life.block("A-1", "blocked-for-test")
            self.assertEqual(life.eligible_tasks(), ["A-2"])


if __name__ == "__main__":
    unittest.main()
