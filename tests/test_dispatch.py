"""Executor dispatch: monotonic generations, exact prompt envelope, strict status settlement,
and the structural rule that an executor launch can end only at ``implemented``."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline_core.adapters import AdapterError, LaunchResult
from pipeline_core.dispatch import DispatchRequest, dispatch_executor
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.reports import (
    ReportError,
    launch_artifacts,
    parse_executor_status,
    parse_status_envelope,
    settle_executor_status,
)
from pipeline_core.state import ACTOR_EXECUTOR, ACTOR_RUNNER, Run, TransitionError
from schemas.contracts import TaskSpec


# --- fixtures --------------------------------------------------------------------------------


def _spec(**overrides: object) -> TaskSpec:
    base: dict[str, object] = dict(
        id="RDS-04",
        title="Persist executor launch generations and reports",
        path="docs/plans/tasks/RDS-04_executor-launch-artifacts.md",
        task_type="python",
        executor="python-executor",
        allowed_scope=("feature-pipeline-skill/pipeline_core/dispatch.py",),
        out_of_scope=("feature-pipeline-skill/pipeline_core/worktree.py",),
        required_skills=(".agents/skills/software-development/feature-pipeline/SKILL.md",),
        verification_commands=(
            {"cwd": "feature-pipeline-skill", "command": "uv run python -m unittest"},
        ),
        max_repair_attempts=2,
    )
    base.update(overrides)
    return TaskSpec.build(**base)  # type: ignore[arg-type]


def _prose(status: str = "implemented", *, with_line: bool = True) -> str:
    lines = ["# Executor report — RDS-04", ""]
    if with_line:
        lines += [f"- Status: {status}", ""]
    lines += ["## Files changed", "- feature-pipeline-skill/pipeline_core/dispatch.py", ""]
    return "\n".join(lines)


def _envelope(status: str, task_id: str = "RDS-04", attempt: int = 1) -> str:
    return json.dumps(
        {"role": "executor", "status": status, "task_id": task_id, "attempt": attempt}
    )


class ScriptedAdapter:
    """A deterministic :class:`~pipeline_core.adapters.Adapter` for the two-call dispatch shape.

    The first ``launch`` is the executor; a launch carrying ``no_tools``/``resume_session_id``
    is the same-session status-envelope continuation.
    """

    def __init__(
        self,
        *,
        prose_status: str = "implemented",
        prose_text: str | None = None,
        envelope_status: str = "implemented",
        envelope_text: str | None = None,
        session_id: str | None = "sess-1",
        launch_exit: int = 0,
        envelope_exit: int = 0,
        write_report: bool = True,
        write_envelope: bool = True,
        raise_code: str | None = None,
    ) -> None:
        self.prose_status = prose_status
        self.prose_text = prose_text
        self.envelope_status = envelope_status
        self.envelope_text = envelope_text
        self.session_id = session_id
        self.launch_exit = launch_exit
        self.envelope_exit = envelope_exit
        self.write_report = write_report
        self.write_envelope = write_envelope
        self.raise_code = raise_code
        self.calls: list[dict] = []

    def launch(self, request):  # noqa: ANN001 - test double
        self.calls.append(
            {"role": request.role, "no_tools": request.no_tools,
             "resume": request.resume_session_id, "prompt": request.prompt}
        )
        if request.no_tools or request.resume_session_id:
            text = self.envelope_text if self.envelope_text is not None else _envelope(
                self.envelope_status, request.task_id
            )
            return self._deliver(request, text, self.envelope_exit, self.write_envelope)
        if self.raise_code:
            raise AdapterError("scripted adapter failure", self.raise_code)
        text = self.prose_text if self.prose_text is not None else _prose(self.prose_status)
        return self._deliver(request, text, self.launch_exit, self.write_report)

    def _deliver(self, request, text: str, exit_code: int, write: bool) -> LaunchResult:  # noqa: ANN001
        if write:
            path = Path(request.report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return LaunchResult(exit_code=exit_code, stdout="", session_id=self.session_id)
        return LaunchResult(exit_code=exit_code, stdout=text, session_id=self.session_id)


def _anchors() -> EnvelopeAnchors:
    return EnvelopeAnchors(project_root=".", agents_root=".agents")


def _running_life(root: Path, spec: TaskSpec) -> RunLifecycle:
    prompt = root / "prompts" / "feature.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("feature", encoding="utf-8")
    run = Run.create("dispatch", prompt, None, root / "storage" / "dispatch", root)
    life = RunLifecycle.initialize(run, tasks=[(spec.id, [])])
    life.transition(spec.id, "running", actor=ACTOR_RUNNER)
    return life


def _request(spec: TaskSpec, **overrides: object) -> DispatchRequest:
    base: dict[str, object] = dict(
        spec=spec, role_grant=("read", "run_checks", "write"), anchors=_anchors(),
        execution_mode="separate", plan_path="docs/plans/2026-09-01-core-execution-engine.md",
    )
    base.update(overrides)
    return DispatchRequest(**base)  # type: ignore[arg-type]


# --- reports.py units ----------------------------------------------------------------------------


class ReportParsingTests(unittest.TestCase):
    def test_prose_status_line_parses_with_optional_dash_and_emphasis(self) -> None:
        self.assertEqual(parse_executor_status("- Status: implemented"), "implemented")
        self.assertEqual(parse_executor_status("Status: **blocked**"), "blocked")
        with self.assertRaises(ReportError) as ctx:
            parse_executor_status("the work is done")
        self.assertEqual(ctx.exception.code, "unparseable-report")

    def test_status_envelope_is_strict_about_shape_and_identity(self) -> None:
        self.assertEqual(
            parse_status_envelope(_envelope("implemented"), role="executor",
                                  task_id="RDS-04", attempt=1),
            "implemented",
        )
        for bad in ('{"role": "executor"}', '{not json', _envelope("verified"),
                    _envelope("implemented", "RDS-99"), _envelope("implemented", attempt=2)):
            with self.assertRaises(ReportError) as ctx:
                parse_status_envelope(bad, role="executor", task_id="RDS-04", attempt=1)
            self.assertEqual(ctx.exception.code, "unparseable-status-envelope")

    def test_settle_fails_closed_on_disagreement_and_records_prose_drift(self) -> None:
        with self.assertRaises(ReportError) as ctx:
            settle_executor_status(
                prose_text="- Status: implemented", envelope_text=_envelope("blocked"),
                role="executor", task_id="RDS-04", attempt=1,
            )
        self.assertEqual(ctx.exception.code, "status-envelope-mismatch")

        resolution = settle_executor_status(
            prose_text="no status here", envelope_text=_envelope("implemented"),
            role="executor", task_id="RDS-04", attempt=1,
        )
        self.assertEqual(resolution.token, "implemented")
        self.assertIsNone(resolution.prose_token)
        self.assertIsNotNone(resolution.drift)


# --- generation allocation -----------------------------------------------------------------------


class GenerationTests(unittest.TestCase):
    def test_success_consumes_one_generation_and_owns_its_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(life, _request(spec), ScriptedAdapter())

            self.assertEqual(outcome.generation, 1)
            self.assertEqual(outcome.status, "implemented")
            self.assertTrue(outcome.artifacts.directory.is_dir())
            self.assertTrue(outcome.artifacts.executor_report.is_file())
            self.assertTrue(outcome.artifacts.status_envelope.is_file())
            self.assertTrue(outcome.artifacts.prompt_envelope.is_file())
            self.assertEqual(life.run.task(spec.id).next_executor_launch_generation, 2)

    def test_generation_is_monotonic_across_failure_and_a_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(
                life, _request(spec), ScriptedAdapter(launch_exit=1))

            self.assertEqual(outcome.generation, 1)
            self.assertEqual(outcome.status, "blocked")
            self.assertIsNone(outcome.settled_status)
            self.assertEqual(life.run.task(spec.id).next_executor_launch_generation, 2)

            # A fresh resume from run.json alone keeps the consumed counter.
            reloaded = RunLifecycle.load(life.run.run_dir, root)
            self.assertEqual(
                reloaded.run.task(spec.id).next_executor_launch_generation, 2)
            self.assertEqual(reloaded.consume_launch_generation(spec.id, "executor"), 2)

    def test_failed_launch_preserves_exit_stdout_stderr_and_session_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = ScriptedAdapter(launch_exit=7, write_report=False,
                                      prose_text="partial work\nstderr trace")
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "blocked")
            self.assertTrue(outcome.artifacts.launch_failure.is_file())
            saved = json.loads(outcome.artifacts.launch_failure.read_text(encoding="utf-8"))
            self.assertEqual(saved["exit_code"], 7)
            self.assertEqual(saved["generation"], 1)
            failures = life.run.task(spec.id).external_launch_failures
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["stage"], "executor")

    def test_adapter_error_on_launch_blocks_and_records_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(
                life, _request(spec), ScriptedAdapter(raise_code="adapter-unavailable"))

            self.assertEqual(outcome.status, "blocked")
            self.assertIn("adapter-unavailable", outcome.failure or "")
            self.assertTrue(outcome.artifacts.launch_failure.is_file())
            self.assertEqual(life.run.task(spec.id).next_executor_launch_generation, 2)


# --- prompt envelope ---------------------------------------------------------------------------


class PromptEnvelopeTests(unittest.TestCase):
    def test_envelope_carries_exact_scope_skills_checks_and_report_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(life, _request(spec), ScriptedAdapter())
            text = outcome.artifacts.prompt_envelope.read_text(encoding="utf-8")

        self.assertIn("- Task ID: RDS-04", text)
        self.assertIn("- Task type: python", text)
        self.assertIn(
            "- Allowed scope: feature-pipeline-skill/pipeline_core/dispatch.py", text)
        self.assertIn(
            "- Out of scope: feature-pipeline-skill/pipeline_core/worktree.py", text)
        self.assertIn(
            "- Required skills: .agents/skills/software-development/feature-pipeline/SKILL.md",
            text,
        )
        self.assertIn("- Maximum repair attempts: 2", text)
        self.assertIn("  - feature-pipeline-skill -> uv run python -m unittest", text)
        self.assertIn("- separate", text)
        self.assertIn("- Role grant: read, run_checks, write", text)
        self.assertRegex(text, r"- Report path: .*launch-1/executor-1\.md")
        self.assertIn("- Status: implemented | blocked", text)


# --- status settlement through dispatch -------------------------------------------------------


class StatusSettlementTests(unittest.TestCase):
    def test_agreeing_prose_and_envelope_yield_implemented(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(life, _request(spec), ScriptedAdapter())

            self.assertEqual(outcome.status, "implemented")
            self.assertEqual(life.run.task(spec.id).status, "implemented")
            evidence = life.run.task(spec.id).execution_evidence
            self.assertEqual(evidence["launch_generation"], 1)
            self.assertTrue(
                evidence["executor_report"].endswith("launch-1/executor-1.md"))

    def test_missing_prose_line_falls_back_to_the_envelope_and_records_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = ScriptedAdapter(prose_text=_prose(with_line=False),
                                      envelope_status="implemented")
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "implemented")
            self.assertIsNotNone(outcome.drift)
            drift_events = [e for e in life.run.history if e.get("note") and "envelope status" in e["note"]]
            self.assertTrue(drift_events)

    def test_prose_envelope_disagreement_fails_closed_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = ScriptedAdapter(prose_status="implemented", envelope_status="blocked")
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "blocked")
            self.assertIsNone(outcome.settled_status)
            self.assertIn("status-envelope-mismatch", outcome.failure or "")
            self.assertEqual(life.run.task(spec.id).status, "blocked")

    def test_malformed_status_envelope_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = ScriptedAdapter(envelope_text='{"role": "executor" oops')
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "blocked")
            self.assertIn("unparseable-status-envelope", outcome.failure or "")

    def test_executor_reported_blocked_blocks_without_a_launch_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = ScriptedAdapter(prose_status="blocked", envelope_status="blocked")
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "blocked")
            self.assertEqual(outcome.settled_status, "blocked")
            self.assertFalse(outcome.artifacts.launch_failure.exists())
            self.assertIn("executor reported blocked", life.run.task(spec.id).blocker or "")


# --- state authority -------------------------------------------------------------------------


class StateAuthorityTests(unittest.TestCase):
    def test_dispatch_only_ever_reaches_implemented_or_blocked(self) -> None:
        for adapter, expected in (
            (ScriptedAdapter(), "implemented"),
            (ScriptedAdapter(prose_status="blocked", envelope_status="blocked"), "blocked"),
            (ScriptedAdapter(launch_exit=1), "blocked"),
        ):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec = _spec()
                life = _running_life(root, spec)
                outcome = dispatch_executor(life, _request(spec), adapter)
                self.assertEqual(outcome.status, expected)
                self.assertIn(outcome.status, {"implemented", "blocked"})

    def test_executor_output_cannot_set_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            dispatch_executor(life, _request(spec), ScriptedAdapter())

            with self.assertRaises(TransitionError) as ctx:
                life.run.transition_task(spec.id, "verified", actor=ACTOR_EXECUTOR)
            self.assertEqual(ctx.exception.code, "unauthorized-transition")
            # The runner remains the only actor that can.
            life.run.transition_task(spec.id, "verified", actor=ACTOR_RUNNER)
            self.assertEqual(life.run.task(spec.id).status, "verified")

    def test_reserved_manifest_and_diff_paths_are_not_written_by_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(life, _request(spec), ScriptedAdapter())

            self.assertFalse(outcome.artifacts.implementation_manifest.exists())
            self.assertFalse(outcome.artifacts.implementation_diff.exists())
            implementation = life.run.task(spec.id).execution_evidence["implementation"]
            self.assertIsNone(implementation["manifest"])
            self.assertIsNone(implementation["diff"])
            self.assertTrue(
                implementation["manifest_reserved"].endswith(
                    "launch-1/implementation-manifest-1.json"))


class ArtifactLayoutTests(unittest.TestCase):
    def test_layout_matches_the_required_shape(self) -> None:
        artifacts = launch_artifacts("run-dir", "RDS-04", 3)
        self.assertEqual(
            artifacts.directory.as_posix(), "run-dir/reports/RDS-04/launch-3")
        self.assertEqual(artifacts.executor_report.name, "executor-3.md")
        self.assertEqual(artifacts.status_envelope.name, "executor-envelope-3.json")
        with self.assertRaises(ReportError) as ctx:
            launch_artifacts("run-dir", "RDS-04", 0)
        self.assertEqual(ctx.exception.code, "invalid-launch-generation")


if __name__ == "__main__":
    unittest.main()
