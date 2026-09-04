"""VP-01 — the extracted task / verification / diagnostic application services.

Focused coverage for the one service path the legacy ``pipeline_core`` orchestration modules
now delegate to:

* :class:`feature_pipeline.ports.diagnostics.RepositorySnapshot` invariants;
* :class:`feature_pipeline.application.diagnostic_service.DiagnosticService` — collects
  repository evidence at the boundary and records a collection failure explicitly;
* :class:`feature_pipeline.application.verification_service.VerificationService` and
  :class:`feature_pipeline.application.task_engine.TaskEngine` — no argparse, no concrete
  adapter construction (AC-1), and the legacy names still delegate here (AC-3).

Standard library only.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from feature_pipeline.application import diagnostic_service as diagnostic_service_mod
from feature_pipeline.application import task_engine as task_engine_mod
from feature_pipeline.application import verification_service as verification_service_mod
from feature_pipeline.application.diagnostic_service import (
    DIAGNOSTIC_COLLECTION_FAILED,
    DiagnosticService,
    GitRepositoryInspection,
)
from feature_pipeline.application.task_engine import (
    TaskEngine,
    TaskExecution,
    TaskRunResult,
)
from feature_pipeline.application.verification_service import (
    VerificationRequest,
    VerificationService,
)
from feature_pipeline.ports.diagnostics import RepositorySnapshot
from pipeline_core import execution as execution_mod
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.state import ACTOR_RUNNER, Run
from schemas.contracts import TaskSpec


def _spec(**overrides: object) -> TaskSpec:
    base: dict[str, object] = dict(
        id="VP-01",
        title="extract services",
        path="docs/plans/tasks/VP-01_application-services.md",
        task_type="python",
        executor="python-executor",
        allowed_scope=("src/**",),
        verification_commands=(),
        max_repair_attempts=2,
    )
    base.update(overrides)
    return TaskSpec.build(**base)  # type: ignore[arg-type]


def _running_run(root: Path, spec: TaskSpec) -> Run:
    prompt = root / "prompt.md"
    prompt.write_text("feature", encoding="utf-8")
    run = Run.create("vp01", prompt, None, root / "storage" / "vp01", root)
    life = RunLifecycle.initialize(run, tasks=[(spec.id, [])])
    life.transition(spec.id, "running", actor=ACTOR_RUNNER)
    return run


class _FakeInspection:
    """A :class:`RepositoryInspectionPort` double — returns a fixed snapshot, no subprocess."""

    def __init__(self, snapshot: RepositorySnapshot) -> None:
        self._snapshot = snapshot
        self.calls: list[str] = []

    def snapshot(self, repo_root: str | Path) -> RepositorySnapshot:
        self.calls.append(str(repo_root))
        return self._snapshot


# --- RepositorySnapshot ------------------------------------------------------------------


class RepositorySnapshotTests(unittest.TestCase):
    def test_complete_only_when_neither_field_failed(self) -> None:
        self.assertTrue(RepositorySnapshot(diff="", status="").complete)
        self.assertFalse(
            RepositorySnapshot(diff="", status="", diff_error="git diff exited 128").complete)

    def test_collection_error_joins_every_failed_field(self) -> None:
        snap = RepositorySnapshot(
            diff="", status="", diff_error="diff broke", status_error="status broke")
        self.assertEqual(snap.collection_error, "diff broke; status broke")
        self.assertIsNone(RepositorySnapshot(diff="x", status="y").collection_error)

    def test_an_empty_reason_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RepositorySnapshot(diff="", status="", diff_error="   ")


# --- DiagnosticService ------------------------------------------------------------------


class DiagnosticServiceTests(unittest.TestCase):
    def test_collected_evidence_is_embedded_and_no_failure_marker_is_added(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            run = _running_run(root, spec)
            port = _FakeInspection(
                RepositorySnapshot(diff="diff --git a/x b/x\n+line\n", status=" M x\n"))

            path = DiagnosticService(port).collect(
                run, task_id=spec.id, attempt=1, note="blocked at the limit")

            text = path.read_text(encoding="utf-8")
            self.assertEqual(port.calls, [str(run.repo_root)])
            self.assertIn("+line", text.split("## git diff", 1)[1])
            self.assertIn("blocked at the limit", text)
            self.assertNotIn(DIAGNOSTIC_COLLECTION_FAILED, text)

    def test_a_partial_collection_failure_is_stated_in_the_note_and_the_section(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            run = _running_run(root, spec)
            port = _FakeInspection(
                RepositorySnapshot(
                    diff="diff --git a/x b/x\n", status="",
                    status_error="git status exited 128: not a git repository"))

            path = DiagnosticService(port).collect(run, task_id=spec.id, attempt=2, note="n")

            text = path.read_text(encoding="utf-8")
            self.assertIn(f"{DIAGNOSTIC_COLLECTION_FAILED}: git status exited 128", text)
            self.assertIn("n | " + DIAGNOSTIC_COLLECTION_FAILED, text)
            status_body = text.split("## git status", 1)[1].split("## ", 1)[0]
            self.assertIn("evidence unavailable", status_body.lower())
            # the field that *did* collect is still embedded verbatim.
            self.assertIn("diff --git", text.split("## git diff", 1)[1])

    def test_a_collected_but_empty_section_is_not_the_bare_none_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            run = _running_run(root, spec)

            path = DiagnosticService(_FakeInspection(
                RepositorySnapshot(diff="", status=""))).collect(
                run, task_id=spec.id, attempt=1)

            diff_body = path.read_text(encoding="utf-8").split("## git diff", 1)[1].split(
                "## ", 1)[0]
            self.assertIn("no changes", diff_body.lower())

    def test_default_git_inspection_reports_unavailable_outside_a_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snap = GitRepositoryInspection().snapshot(directory)
            self.assertFalse(snap.complete)
            self.assertIsNotNone(snap.collection_error)


# --- AC-1: no argparse, no concrete adapter construction --------------------------------


class ServicesTakeNoCliOrAdapterTests(unittest.TestCase):
    def _sources(self) -> str:
        return "\n".join(
            inspect.getsource(mod)
            for mod in (
                diagnostic_service_mod,
                verification_service_mod,
                task_engine_mod,
            )
        )

    def test_no_argparse_anywhere_in_the_services(self) -> None:
        src = self._sources()
        self.assertNotIn("import argparse", src)
        self.assertNotIn("argparse.", src)

    def test_no_concrete_claude_adapter_is_constructed(self) -> None:
        src = self._sources()
        self.assertNotIn("ClaudeAdapter(", src)
        self.assertNotIn("ClaudeCliAdapter(", src)
        # the verifier launcher pair and the executor adapter arrive on the request.
        self.assertIn("launchers", inspect.getsource(task_engine_mod.TaskExecution))


# --- AC-3: the legacy names delegate to the one service path ---------------------------


class LegacyDelegationTests(unittest.TestCase):
    def test_run_task_delegates_to_the_task_engine(self) -> None:
        self.assertIs(execution_mod.TaskExecution, TaskExecution)
        self.assertIs(execution_mod.TaskRunResult, TaskRunResult)
        body = inspect.getsource(execution_mod.run_task)
        self.assertIn("TaskEngine().run(life, request)", body)

    def test_task_engine_injects_both_collaborator_services_by_default(self) -> None:
        engine = TaskEngine()
        self.assertIsInstance(engine._verification, VerificationService)
        self.assertIsInstance(engine._diagnostics, DiagnosticService)

    def test_verification_service_sequences_the_three_runner_owned_primitives(self) -> None:
        body = inspect.getsource(VerificationService.verify)
        for call in (
            "run_verification_commands(",
            "build_verification_evidence(",
            "orchestrate_verification(",
        ):
            self.assertIn(call, body)

    def test_verification_request_carries_injected_launchers(self) -> None:
        fields = {f for f in VerificationRequest.__dataclass_fields__}
        self.assertIn("launchers", fields)
        self.assertIn("anchors", fields)


if __name__ == "__main__":
    unittest.main()
