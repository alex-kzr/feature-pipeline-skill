"""Durable run lifecycle: fresh initialization, atomic flush, resume reconciliation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline_core.lifecycle import CORE_VERSION, RunLifecycle
from pipeline_core.state import (
    ACTOR_EXECUTOR,
    ACTOR_HUMAN,
    ACTOR_RUNNER,
    ResumeError,
    Run,
)


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
    def test_failed_operation_keeps_task_in_progress_and_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            life = RunLifecycle.initialize(_run(Path(directory)), tasks=[("A-1", [])])
            life.transition("A-1", "in_progress", actor=ACTOR_RUNNER)
            life.record_operation("A-1", "verification", "failed", "tests failed")
            task = _stored(life.run.run_dir)["tasks"][0]
            self.assertEqual(task["status"], "in_progress")
            self.assertEqual(task["operation_history"][-1]["outcome"], "failed")

    def test_launch_failure_is_an_operation_not_a_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            life = RunLifecycle.initialize(_run(Path(directory)), tasks=[("A-1", [])])
            life.transition("A-1", "in_progress", actor=ACTOR_RUNNER)
            life.run.record_launch_failure(
                "A-1", stage="executor", generation=1, exit_code=1,
                detail="adapter unavailable",
            )
            task = life.run.task("A-1")
            self.assertEqual(task.status, "in_progress")
            self.assertEqual(task.operation_history[-1]["kind"], "executor")
            self.assertEqual(task.operation_history[-1]["outcome"], "failed")

    def test_exhausted_repair_budget_is_an_escalation_that_keeps_work_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            life = RunLifecycle.initialize(_run(Path(directory)), tasks=[("A-1", [])])
            life.transition("A-1", "in_progress", actor=ACTOR_RUNNER)
            self.assertFalse(life.run.begin_repair("A-1", maximum=0))
            task = life.run.task("A-1")
            self.assertEqual(task.status, "in_progress")
            self.assertEqual(task.operation_history[-1]["outcome"], "escalated")

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

    def test_initialize_keeps_new_tasks_to_do(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            RunLifecycle.initialize(run, tasks=[("A-1", []), ("A-2", ["A-1"])])
            tasks = {t["id"]: t["status"] for t in _stored(run.run_dir)["tasks"]}
            self.assertEqual(tasks, {"A-1": "to_do", "A-2": "to_do"})


class AtomicFlushTests(unittest.TestCase):
    def _life(self, root: Path) -> RunLifecycle:
        return RunLifecycle.initialize(_run(root), tasks=[("A-1", [])])

    def test_transition_flushes_and_leaves_a_history_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = self._life(root)
            life.transition("A-1", "in_progress", actor=ACTOR_RUNNER)
            stored = _stored(life.run.run_dir)
            self.assertEqual(
                [t["status"] for t in stored["tasks"] if t["id"] == "A-1"], ["in_progress"])
            self.assertEqual(stored["history"][-1]["to"], "in_progress")
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

    def test_human_operational_unblock_records_a_resumable_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = RunLifecycle.initialize(_run(root), tasks=[("A-1", []), ("A-2", ["A-1"])])
            life.transition("A-1", "running", actor=ACTOR_RUNNER)
            life.run.task("A-1").attempts = 1
            life.run.task("A-1").execution_evidence["executor_report"] = "reports/A-1/report.md"
            life.block("A-1", "external operational: uv cache access denied")
            life.reopen_operational_block(
                "A-1", authorization="human-authorized-operational-unblock",
                cache_path=".pipeline/uv-cache",
            )

            reopened = Run.load(life.run.run_dir, root)
            self.assertEqual(reopened.task("A-1").status, "in_progress")
            self.assertEqual(reopened.task("A-1").attempts, 1)
            self.assertEqual(
                reopened.task("A-1").execution_evidence["executor_report"],
                "reports/A-1/report.md",
            )
            operation = reopened.task("A-1").operation_history[-1]
            self.assertEqual(operation["kind"], "resume")
            self.assertEqual(operation["outcome"], "retryable")
            self.assertEqual(reopened.task("A-2").status, "to_do")


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

    def test_in_progress_task_remains_resumable_and_clears_the_in_flight_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._interrupted(root, "running")
            life = self._resume(root, run)
            self.assertEqual(life.run.task("A-1").status, "in_progress")
            self.assertIsNone(life.run.current_task)

    def test_repairing_normalizes_to_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._interrupted(root, "repairing")
            life = self._resume(root, run)
            self.assertEqual(life.run.task("A-1").status, "in_progress")

    def test_legacy_execution_phases_normalize_on_resume(self) -> None:
        for status, expected in (("implemented", "in_progress"), ("verified", "done")):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = self._interrupted(root, status)
                life = self._resume(root, run)
                self.assertEqual(life.run.task("A-1").status, expected)

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

    def _superseded_chain(self, root: Path) -> Run:
        """TC-01 -> TC-02 -> TC-03 verified, TC-04 blocked (its work superseded by REC-01)."""
        run = _run(root, plan="plan.md")
        life = RunLifecycle.initialize(
            run,
            tasks=[("TC-01", []), ("TC-02", ["TC-01"]), ("TC-03", ["TC-02"]),
                   ("TC-04", ["TC-03"])],
        )
        for task_id in ("TC-01", "TC-02", "TC-03"):
            if life.run.task(task_id).status == "pending":
                life.transition(task_id, "ready", actor=ACTOR_RUNNER)
            life.transition(task_id, "running", actor=ACTOR_RUNNER)
            life.transition(task_id, "implemented", actor=ACTOR_EXECUTOR)
            life.transition(task_id, "verified", actor=ACTOR_RUNNER)
            life.recompute_readiness()
        life.transition("TC-04", "running", actor=ACTOR_RUNNER)
        life.block("TC-04", "max-repair-attempts-exhausted")
        return run

    def test_resume_rejects_an_unfinished_predecessor_out_of_scope(self) -> None:
        # Operational waits are not terminal.  Omitting that unfinished task therefore remains
        # a scope mismatch.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._superseded_chain(root)
            with self.assertRaises(ResumeError) as caught:
                RunLifecycle.resume(
                    run.run_dir, root, feature="durable",
                    prompt_path=root / "prompts" / "feature.md", plan_path=root / "plan.md",
                    expected_tasks={"TC-01": [], "TC-02": ["TC-01"], "TC-03": ["TC-02"]})
            self.assertEqual(caught.exception.code, "task-set-mismatch")

    def test_resume_still_rejects_a_non_terminal_task_dropped_from_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._superseded_chain(root)
            # TC-04 already holds an unfinished operation, so dropping it is a real mismatch.
            with self.assertRaises(ResumeError) as caught:
                RunLifecycle.resume(
                    run.run_dir, root, feature="durable",
                    prompt_path=root / "prompts" / "feature.md",
                    plan_path=root / "plan.md",
                    expected_tasks={"TC-01": [], "TC-02": ["TC-01"], "TC-03": ["TC-02"]})
            self.assertEqual(caught.exception.code, "task-set-mismatch")

    def test_resume_still_rejects_an_edge_change_inside_the_resumed_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._superseded_chain(root)
            with self.assertRaises(ResumeError) as caught:
                RunLifecycle.resume(
                    run.run_dir, root, feature="durable",
                    prompt_path=root / "prompts" / "feature.md",
                    plan_path=root / "plan.md",
                    expected_tasks={"TC-01": [], "TC-02": [], "TC-03": ["TC-02"]})
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
            self.assertEqual(reloaded.run.task("A-2").status, "to_do")

    def test_a_stale_ready_flag_is_not_trusted_when_a_dependency_regressed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            life = RunLifecycle.initialize(run, tasks=[("A-1", []), ("A-2", ["A-1"])])
            # Force an inconsistent snapshot: A-2 marked ready while A-1 never verified.
            life.run.task("A-2").status = "to_do"
            life.recompute_readiness()
            self.assertEqual(life.run.task("A-2").status, "to_do")

    def test_operational_wait_does_not_suppress_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            life = RunLifecycle.initialize(
                run, tasks=[("A-1", []), ("A-2", ["A-1"]), ("A-3", ["A-2"])])
            life.block("A-1", "max-repair-attempts-exhausted")
            self.assertEqual(life.run.task("A-1").status, "in_progress")
            self.assertIsNone(life.run.task("A-2").blocker)
            self.assertIsNone(life.run.task("A-3").blocker)
            self.assertIn("A-3", life.run.tasks)
            self.assertIn("A-2", life.eligible_tasks())

    def test_an_attested_dependency_satisfies_readiness_without_being_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            life = RunLifecycle.initialize(run, tasks=[("A-1", []), ("A-2", ["A-1"])])
            life.run.record_attestation(
                "A-2", "A-1",
                {"dep_id": "A-1", "source_feature": "elsewhere", "task_verdict": "PASS"})
            life.recompute_readiness()
            self.assertEqual(life.run.task("A-2").status, "to_do")
            # A-1 itself is untouched by the attestation on its dependent.
            self.assertEqual(life.run.task("A-1").status, "to_do")

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
            self.assertEqual(life.run.task("A-3").status, "to_do")

    def test_eligible_tasks_are_ready_and_unblocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            life = RunLifecycle.initialize(run, tasks=[("A-1", []), ("A-2", [])])
            life.block("A-1", "blocked-for-test")
            self.assertEqual(life.eligible_tasks(), ["A-2"])


if __name__ == "__main__":
    unittest.main()
