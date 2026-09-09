"""LR-01 — a recovery source is read-only, launch-failed, and pre-implementation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from feature_pipeline.contracts import TaskSpec
from pipeline_core.adapters import AdapterError, LaunchResult
from pipeline_core.concurrency import pipeline_lock_path, task_lock_path
from pipeline_core.execution import ExecuteControls, ExecuteRequest, ExecutionError, execute_run, recovery_provenance
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.state import ACTOR_RUNNER, Run
from pipeline_core.verification import VerifierAnchors, VerifierLaunchers


class RecoveryProvenanceTests(unittest.TestCase):
    def test_production_recovery_initializes_from_a_real_launch_failure_without_mutating_source(self) -> None:
        class FailingCodex:
            name = "codex"

            def launch(self, request: object) -> LaunchResult:
                raise AdapterError("external launch failed", "launch-failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            plan = root / "plan.md"
            prompt.write_text("prompt\n", encoding="utf-8")
            plan.write_text("plan\n", encoding="utf-8")
            adapter = FailingCodex()
            source_request = self._execute_request(
                root, "source", prompt, plan, adapter,
                ExecuteControls(plan_approved=True, adapter="codex", adapter_explicit=True, task="LR-01"),
            )
            source_result = execute_run(source_request)
            self.assertEqual(source_result.status, "retryable")
            source_dir = source_request.run_dir
            self.assertTrue((source_dir / "reports" / "LR-01" / "launch-1" / "executor-prompt-1.md").is_file())
            before = {path.relative_to(source_dir): path.read_bytes() for path in source_dir.rglob("*") if path.is_file()}

            replacement_result = execute_run(self._execute_request(
                root, "source-recovery-codex", prompt, plan, adapter,
                ExecuteControls(
                    plan_approved=True, adapter="codex", adapter_explicit=True, task="LR-01",
                    recovery_source_feature="source", recovery_task="LR-01",
                ),
            ))

            self.assertEqual(replacement_result.status, "retryable", replacement_result.message)
            replacement = Run.load(root / ".pipeline" / "runs" / "source-recovery-codex", root)
            self.assertEqual(replacement.recovery["source_run_id"], Run.load(source_dir, root).run_id)
            after = {path.relative_to(source_dir): path.read_bytes() for path in source_dir.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_recovery_rejects_non_codex_target_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_launch_failed_source(root)

            with self.assertRaisesRegex(ExecutionError, "Codex"):
                recovery_provenance(
                    controls=ExecuteControls(
                        recovery_source_feature="source", recovery_task="LR-01",
                        adapter="claude", adapter_explicit=True, task="LR-01",
                    ),
                    run_dir=root / ".pipeline" / "runs" / "source-claude-recovery",
                    repo_root=root,
                    specs=(self._spec(),),
                )

    def test_recovery_rejects_source_with_live_pipeline_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_launch_failed_source(root)
            lock = pipeline_lock_path(root)
            lock.parent.mkdir(parents=True)
            lock.write_text('{"pid": 1}\n', encoding="utf-8")

            with self.assertRaisesRegex(ExecutionError, "writer lease"):
                recovery_provenance(
                    controls=ExecuteControls(
                        recovery_source_feature="source", recovery_task="LR-01",
                        adapter="codex", adapter_explicit=True, task="LR-01",
                    ),
                    run_dir=root / ".pipeline" / "runs" / "source-recovery-codex",
                    repo_root=root,
                    specs=(self._spec(),),
                )

    def test_eligible_launch_failure_returns_immutable_source_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            plan = root / "plan.md"
            prompt.write_text("prompt\n", encoding="utf-8")
            plan.write_text("plan\n", encoding="utf-8")
            source_dir = self._write_launch_failed_source(root, prompt=prompt, plan=plan)
            before = {
                path.relative_to(source_dir): path.read_bytes()
                for path in source_dir.rglob("*") if path.is_file()
            }

            provenance = recovery_provenance(
                controls=ExecuteControls(
                    recovery_source_feature="source", recovery_task="LR-01",
                    adapter="codex", adapter_explicit=True, task="LR-01",
                ),
                run_dir=root / ".pipeline" / "runs" / "source-recovery-codex",
                repo_root=root,
                specs=(self._spec(),),
            )

            self.assertEqual(provenance["source_feature"], "source")
            self.assertEqual(provenance["source_task"], "LR-01")
            self.assertEqual(provenance["target_adapter"], "codex")
            self.assertTrue(provenance["launch_failure_digest"].startswith("sha256:"))
            after = {
                path.relative_to(source_dir): path.read_bytes()
                for path in source_dir.rglob("*") if path.is_file()
            }
            self.assertEqual(after, before)

    def test_recovery_rejects_an_arbitrary_replacement_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_launch_failed_source(root)

            with self.assertRaisesRegex(ExecutionError, "replacement identity"):
                recovery_provenance(
                    controls=self._controls(),
                    run_dir=root / ".pipeline" / "runs" / "caller-chosen-output",
                    repo_root=root,
                    specs=(self._spec(),),
                )

    def test_recovery_rejects_an_occupied_deterministic_replacement_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_launch_failed_source(root)
            replacement_dir = root / ".pipeline" / "runs" / "source-recovery-codex"
            replacement = Run.create(
                "source-recovery-codex", root / "prompt.md", root / "plan.md",
                replacement_dir, root,
            )
            replacement.save()

            with self.assertRaisesRegex(ExecutionError, "replacement identity"):
                recovery_provenance(
                    controls=self._controls(),
                    run_dir=replacement_dir,
                    repo_root=root,
                    specs=(self._spec(),),
                )

    def test_recovery_rejects_every_source_execution_or_artifact_boundary(self) -> None:
        cases = {
            "terminal task state": lambda run: setattr(run.task("LR-01"), "status", "implemented"),
            "executor report": lambda run: run.task("LR-01").execution_evidence.__setitem__(
                "executor_report", "reports/LR-01/executor.md"),
            "command evidence": lambda run: run.commands.append({"id": "command-1"}),
            "stage evidence": lambda run: run.stages.__setitem__("executor", {"commands": []}),
            "verifier evidence": lambda run: run.task("LR-01").verification.__setitem__(
                "task_verdict", "PASS"),
            "retained artifact": lambda run: run.artifacts.__setitem__("executor", "report.md"),
            "recovery provenance": lambda run: setattr(run, "recovery", {"source_feature": "older"}),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_dir = self._write_launch_failed_source(root)
                source = Run.load(source_dir, root)
                mutate(source)
                source.save()

                with self.assertRaises(ExecutionError):
                    recovery_provenance(
                        controls=self._controls(),
                        run_dir=root / ".pipeline" / "runs" / "source-recovery-codex",
                        repo_root=root,
                    specs=(self._spec(),),
                    )

    def test_recovery_rejects_a_terminal_source_run_with_a_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = self._write_launch_failed_source(root)
            source = Run.load(source_dir, root)
            source.status = "blocked"
            source.save()

            with self.assertRaisesRegex(ExecutionError, "terminal run state"):
                recovery_provenance(
                    controls=self._controls(),
                    run_dir=root / ".pipeline" / "runs" / "source-recovery-codex",
                    repo_root=root,
                    specs=(self._spec(),),
                )

    def test_recovery_rejects_an_unrecorded_source_verifier_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = self._write_launch_failed_source(root)
            report = source_dir / "reports" / "LR-01" / "verify-1" / "task-verifier-1.md"
            report.parent.mkdir(parents=True)
            report.write_text("Verdict: FAIL\n", encoding="utf-8")

            with self.assertRaisesRegex(ExecutionError, "retained execution artifacts"):
                recovery_provenance(
                    controls=self._controls(),
                    run_dir=root / ".pipeline" / "runs" / "source-recovery-codex",
                    repo_root=root,
                    specs=(self._spec(),),
                )

    def test_recovery_rejects_logs_only_and_nested_retained_source_files_immutably(self) -> None:
        cases = (
            ("logs/executor.log", "logs only"),
            ("retained/nested/evidence.json", "nested file"),
            ("declared/reference.txt", "declared artifact reference"),
        )
        for relative_path, name in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_dir = self._write_launch_failed_source(root)
                retained = source_dir / relative_path
                retained.parent.mkdir(parents=True)
                retained.write_text("retained\n", encoding="utf-8")
                if name == "declared artifact reference":
                    source = Run.load(source_dir, root)
                    source.artifacts["retained"] = relative_path
                    source.save()
                before = {
                    path.relative_to(source_dir): path.read_bytes()
                    for path in source_dir.rglob("*") if path.is_file()
                }

                with self.assertRaisesRegex(ExecutionError, "retained execution artifacts"):
                    recovery_provenance(
                        controls=self._controls(),
                        run_dir=root / ".pipeline" / "runs" / "source-recovery-codex",
                        repo_root=root,
                        specs=(self._spec(),),
                    )

                after = {
                    path.relative_to(source_dir): path.read_bytes()
                    for path in source_dir.rglob("*") if path.is_file()
                }
                self.assertEqual(after, before)

    def test_recovery_rejects_every_noncanonical_launch_failure_artifact(self) -> None:
        cases = (
            ("reports/LR-01/launch-1/executor-1.md", "executor report"),
            ("reports/LR-01/launch-1/executor-envelope-1.json", "executor envelope"),
            ("reports/LR-01/launch-1/result-protocol-invalid-1.json", "result envelope"),
            ("reports/LR-01/launch-1/implementation-manifest-1.json", "implementation manifest"),
            ("reports/LR-01/launch-1/implementation-diff-1.md", "implementation diff"),
            ("logs/command-1.log", "command log"),
            ("reports/LR-01/verify-1/task-verifier-1.md", "verifier report"),
            ("reports/LR-01/launch-1/nested/unlisted.json", "nested file"),
        )
        for relative_path, name in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_dir = self._write_launch_failed_source(root)
                extra = source_dir / relative_path
                extra.parent.mkdir(parents=True, exist_ok=True)
                extra.write_text("forbidden\n", encoding="utf-8")

                with self.assertRaisesRegex(ExecutionError, "retained execution artifacts"):
                    recovery_provenance(
                        controls=self._controls(),
                        run_dir=root / ".pipeline" / "runs" / "source-recovery-codex",
                        repo_root=root,
                        specs=(self._spec(),),
                    )

    def test_recovery_rejects_missing_mismatched_or_duplicate_launch_failure_evidence(self) -> None:
        cases = ("missing", "mismatched", "duplicate")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_dir = self._write_launch_failed_source(root)
                diagnostic = source_dir / "reports" / "LR-01" / "launch-1" / "launch-failure-1.json"
                if case == "missing":
                    diagnostic.unlink()
                elif case == "mismatched":
                    diagnostic.write_text(json.dumps({
                        "task_id": "OTHER", "generation": 1, "stage": "executor",
                        "exit_code": 1, "reason": "session limit before implementation",
                    }), encoding="utf-8")
                else:
                    source = Run.load(source_dir, root)
                    source.record_launch_failure(
                        "LR-01", stage="executor", generation=2, exit_code=1,
                        detail="second failure",
                    )
                    source.save()

                with self.assertRaises(ExecutionError):
                    recovery_provenance(
                        controls=self._controls(),
                        run_dir=root / ".pipeline" / "runs" / "source-recovery-codex",
                        repo_root=root,
                        specs=(self._spec(),),
                    )

    def test_recovery_rejects_a_diagnostic_bound_to_another_source_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = self._write_launch_failed_source(root)
            other_dir = self._write_launch_failed_source(root, feature="other")
            diagnostic = source_dir / "reports" / "LR-01" / "launch-1" / "launch-failure-1.json"
            payload = json.loads(diagnostic.read_text(encoding="utf-8"))
            payload["source_run_id"] = Run.load(other_dir, root).run_id
            diagnostic.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ExecutionError, "does not match its record"):
                recovery_provenance(
                    controls=self._controls(),
                    run_dir=root / ".pipeline" / "runs" / "source-recovery-codex",
                    repo_root=root,
                    specs=(self._spec(),),
                )

    def test_recovery_rejects_duplicate_replacement_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_launch_failed_source(root)
            replacement_dir = root / ".pipeline" / "runs" / "older-replacement"
            replacement = Run.create("older-replacement", root / "prompt.md", root / "plan.md",
                                     replacement_dir, root)
            replacement.recovery = {"source_feature": "source", "source_task": "LR-01"}
            replacement.save()

            with self.assertRaisesRegex(ExecutionError, "already has a replacement"):
                recovery_provenance(
                    controls=self._controls(),
                    run_dir=root / ".pipeline" / "runs" / "source-recovery-codex",
                    repo_root=root,
                    specs=(self._spec(),),
                )

    def test_recovery_rejects_a_live_task_writer_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_launch_failed_source(root)
            lock = task_lock_path(root, "LR-01")
            lock.parent.mkdir(parents=True)
            lock.write_text('{"pid": 1}\n', encoding="utf-8")

            with self.assertRaisesRegex(ExecutionError, "writer lease"):
                recovery_provenance(
                    controls=self._controls(),
                    run_dir=root / ".pipeline" / "runs" / "source-recovery-codex",
                    repo_root=root,
                    specs=(self._spec(),),
                )

    @staticmethod
    def _spec() -> TaskSpec:
        return TaskSpec.build(
            id="LR-01", task_type="tooling", executor="executor",
            allowed_scope=["src/x.py"], acceptance_criteria=["done"],
        )

    def _execute_request(
        self, root: Path, feature: str, prompt: Path, plan: Path, adapter: object,
        controls: ExecuteControls,
    ) -> ExecuteRequest:
        return ExecuteRequest(
            feature=feature, repo_root=root, run_dir=root / ".pipeline" / "runs" / feature,
            prompt_path=prompt, plan_path=plan, specs=(self._spec(),), adapter=adapter,
            launchers=VerifierLaunchers(task=adapter, test=adapter),
            envelope_anchors=EnvelopeAnchors(project_root=".", agents_root=".agents"),
            verifier_anchors=VerifierAnchors(project_root=str(root), agents_root=str(root / ".agents")),
            environment={"codex": True}, controls=controls,
        )

    @staticmethod
    def _controls() -> ExecuteControls:
        return ExecuteControls(
            recovery_source_feature="source", recovery_task="LR-01",
            adapter="codex", adapter_explicit=True, task="LR-01",
        )

    @staticmethod
    def _write_launch_failed_source(
        root: Path, *, feature: str = "source", prompt: Path | None = None, plan: Path | None = None,
    ) -> Path:
        prompt = prompt or root / "prompt.md"
        plan = plan or root / "plan.md"
        prompt.write_text("prompt\n", encoding="utf-8")
        plan.write_text("plan\n", encoding="utf-8")
        source_dir = root / ".pipeline" / "runs" / feature
        source = Run.create(feature, prompt, plan, source_dir, root)
        source_life = RunLifecycle.initialize(source, tasks=[("LR-01", [])])
        source_life.transition("LR-01", "running", actor=ACTOR_RUNNER)
        prompt_envelope = source_dir / "reports" / "LR-01" / "launch-1" / "executor-prompt-1.md"
        prompt_envelope.parent.mkdir(parents=True, exist_ok=True)
        prompt_envelope.write_text("canonical launch prompt\n", encoding="utf-8")
        diagnostic = source_dir / "reports" / "LR-01" / "launch-1" / "launch-failure-1.json"
        diagnostic.write_text(json.dumps({
            "task_id": "LR-01", "generation": 1, "stage": "executor",
            "exit_code": 1, "reason": "session limit before implementation",
        }), encoding="utf-8")
        source.record_launch_failure(
            "LR-01", stage="executor", generation=1, exit_code=1,
            detail="session limit before implementation",
        )
        source.save()
        return source_dir


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
