"""Executor dispatch: monotonic generations, exact prompt envelope, strict status settlement,
and the structural rule that an executor launch can end only at ``implemented``."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline_core.adapters import (
    AdapterError,
    ClaudeAdapter,
    CodexAdapter,
    CompletedProcess,
    LaunchResult,
)
from pipeline_core.dispatch import DispatchError, DispatchRequest, dispatch_executor as _dispatch_executor
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.prompt_envelope import EnvelopeAnchors, build_executor_envelope
from pipeline_core.reports import (
    ReportError,
    launch_artifacts,
    parse_executor_status,
    parse_status_envelope,
    settle_executor_status,
)
from pipeline_core.state import ACTOR_EXECUTOR, ACTOR_RUNNER, Run, TransitionError
from feature_pipeline.contracts import TaskSpec
from feature_pipeline.application.work_items import activate_work_item, register_work_items


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


def _envelope(status: str, task_id: str = "RDS-04", attempt: int = 1, **extra: object) -> str:
    body = {"role": "executor", "status": status, "task_id": task_id, "attempt": attempt}
    body.update(extra)
    return json.dumps(body)


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
        write_report: bool = False,
        write_envelope: bool = True,
        written_report_text: str | None = None,
        raise_code: str | None = None,
        on_launch=None,
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
        self.written_report_text = written_report_text
        self.raise_code = raise_code
        #: Optional callable invoked once, on the executor launch, to simulate the executor
        #: mutating the workspace inside its window.
        self.on_launch = on_launch
        self.calls: list[dict] = []

    def launch(self, request):  # noqa: ANN001 - test double
        self.calls.append(
            {"role": request.role, "no_tools": request.no_tools,
             "resume": request.resume_session_id, "prompt": request.prompt,
             "allowed_tools": request.allowed_tools}
        )
        if request.no_tools or request.resume_session_id:
            text = self.envelope_text if self.envelope_text is not None else _envelope(
                self.envelope_status, request.task_id
            )
            return self._deliver(request, text, self.envelope_exit, self.write_envelope)
        if self.raise_code:
            raise AdapterError("scripted adapter failure", self.raise_code)
        if self.on_launch is not None:
            self.on_launch(request)
        text = self.prose_text if self.prose_text is not None else _prose(self.prose_status)
        return self._deliver(
            request, text, self.launch_exit, self.write_report, self.written_report_text)

    def _deliver(
        self, request, text: str, exit_code: int, write: bool, written_text: str | None = None,
    ) -> LaunchResult:  # noqa: ANN001
        if write:
            path = Path(request.report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(written_text if written_text is not None else text, encoding="utf-8")
        return LaunchResult(exit_code=exit_code, stdout=text, session_id=self.session_id)


class CodexFinalAdapter:
    """Codex-shaped launch whose raw JSONL, not report prose, owns the terminal result."""

    name = "codex"

    def __init__(self, payloads: list[object], *, exit_code: int = 0) -> None:
        self.payloads = payloads
        self.exit_code = exit_code

    def launch(self, request):  # noqa: ANN001 - test double
        events = [json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(payload),
        }}) for payload in self.payloads]
        events.append(json.dumps({"type": "turn.completed"}))
        return LaunchResult(self.exit_code, "# Human report\n", "", "thread-1", "\n".join(events))


def _anchors() -> EnvelopeAnchors:
    return EnvelopeAnchors(project_root=".", agents_root=".agents")


def _running_life(root: Path, spec: TaskSpec) -> RunLifecycle:
    prompt = root / "prompts" / "feature.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("feature", encoding="utf-8")
    run = Run.create("dispatch", prompt, None, root / "storage" / "dispatch", root)
    life = RunLifecycle.initialize(run, tasks=[(spec.id, [])])
    register_work_items(run, (spec,))
    life.transition(spec.id, "running", actor=ACTOR_RUNNER)
    return life


def _request(spec: TaskSpec, **overrides: object) -> DispatchRequest:
    base: dict[str, object] = dict(
        spec=spec, role_grant=("read", "run_checks", "write"), anchors=_anchors(),
        execution_mode="separate", plan_path="docs/plans/2026-09-01-core-execution-engine.md",
    )
    base.update(overrides)
    return DispatchRequest(**base)  # type: ignore[arg-type]


def dispatch_executor(life: RunLifecycle, request: DispatchRequest, adapter: object):
    """Dispatch through the explicit producer-activation boundary used in production."""
    with activate_work_item(life.run, request.spec.id):
        return _dispatch_executor(life, request, adapter)  # type: ignore[arg-type]


# --- reports.py units ----------------------------------------------------------------------------


class ReportParsingTests(unittest.TestCase):
    def test_prose_status_line_parses_with_optional_dash_and_emphasis(self) -> None:
        self.assertEqual(parse_executor_status("- Status: implemented"), "implemented")
        self.assertEqual(parse_executor_status("Status: **blocked**"), "blocked")
        with self.assertRaises(ReportError) as ctx:
            parse_executor_status("the work is done")
        self.assertEqual(ctx.exception.code, "unparseable-report")


class ProducerAttributionTests(unittest.TestCase):
    def test_direct_unregistered_executor_launch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            prompt = root / "prompt.md"; prompt.write_text("x", encoding="utf-8")
            run = Run.create("direct", prompt, None, root / "runs" / "direct", root)
            life = RunLifecycle.initialize(run, tasks=[(spec.id, ())])
            life.transition(spec.id, "running", actor=ACTOR_RUNNER)
            with self.assertRaises(DispatchError) as raised:
                _dispatch_executor(life, _request(spec), ScriptedAdapter())
            self.assertEqual(raised.exception.code, "unregistered-producer")

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
                prose_text="- Status: implemented",
                envelope_text=_envelope("blocked", reason="declared check unavailable"),
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
    def test_runner_uses_generic_adapter_stdout_when_executor_writes_report_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            captured = _prose()
            outcome = dispatch_executor(
                life,
                _request(spec),
                ScriptedAdapter(
                    prose_text=captured,
                    write_report=True,
                    written_report_text="executor-controlled artifact\n",
                ),
            )

            self.assertEqual(outcome.status, "implemented")
            self.assertEqual(outcome.artifacts.executor_report.read_text(encoding="utf-8"), captured)

    def test_runner_persists_generic_adapter_stdout_without_adapter_report_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(
                life, _request(spec), ScriptedAdapter(write_report=False))

            self.assertEqual(outcome.status, "implemented")
            self.assertEqual(outcome.artifacts.executor_report.read_text(encoding="utf-8"), _prose())

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
            self.assertEqual(outcome.status, "retryable")
            self.assertEqual(life.run.task(spec.id).status, "in_progress")
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

            self.assertEqual(outcome.status, "retryable")
            self.assertEqual(life.run.task(spec.id).status, "in_progress")
            self.assertTrue(outcome.artifacts.launch_failure.is_file())
            saved = json.loads(outcome.artifacts.launch_failure.read_text(encoding="utf-8"))
            self.assertEqual(saved["exit_code"], 7)
            self.assertEqual(saved["generation"], 1)
            failures = life.run.task(spec.id).external_launch_failures
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["stage"], "executor")

    def test_adapter_error_on_launch_is_retryable_and_records_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(
                life, _request(spec), ScriptedAdapter(raise_code="adapter-unavailable"))

            self.assertEqual(outcome.status, "retryable")
            self.assertEqual(life.run.task(spec.id).status, "in_progress")
            self.assertIn("adapter-unavailable", outcome.failure or "")
            self.assertTrue(outcome.artifacts.launch_failure.is_file())
            self.assertEqual(life.run.task(spec.id).next_executor_launch_generation, 2)


# --- adapter-facing role resolution (RDS-06) -----------------------------------------------------


class CheckGrantDerivationTests(unittest.TestCase):
    """RLC-01 AC-1: the executor launch is granted exact ``Bash(<argv>)`` allowances derived
    only from this task's own declared, working-root-matching verification commands."""

    def test_executor_launch_receives_only_its_own_qualifying_check_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec(
                verification_commands=(
                    {"cwd": ".", "command": "uv run python -m unittest"},
                    {"cwd": "elsewhere", "command": "uv run pytest"},
                ),
            )
            life = _running_life(root, spec)
            adapter = ScriptedAdapter()
            dispatch_executor(life, _request(spec, working_root="."), adapter)

            self.assertEqual(
                adapter.calls[0]["allowed_tools"],
                ("Bash(uv run python -m unittest)",),
            )
            # The same-session status-envelope continuation is tool-free regardless.
            self.assertEqual(adapter.calls[1]["allowed_tools"], ())


class AdapterRoleResolutionTests(unittest.TestCase):
    """``dispatch_executor`` must launch the task's own concrete ``Executor``, not the generic
    ``EXECUTOR_ROLE`` stage constant — the Claude CLI has no ``executor`` agent."""

    def test_both_adapter_launches_use_the_spec_executor_role(self) -> None:
        for executor in ("python-executor", "docs-maintainer"):
            with self.subTest(executor=executor):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    spec = _spec(executor=executor)
                    life = _running_life(root, spec)
                    adapter = ScriptedAdapter()
                    outcome = dispatch_executor(life, _request(spec), adapter)

                    self.assertEqual(outcome.status, "implemented")
                    self.assertEqual(len(adapter.calls), 2)
                    self.assertEqual(adapter.calls[0]["role"], executor)
                    self.assertEqual(adapter.calls[1]["role"], executor)
                    self.assertNotEqual(adapter.calls[0]["role"], "executor")


# --- result-text extraction through a real ClaudeAdapter (RDS-07) --------------------------------


_WRAPPED_CLAUDE_FAKE = """\
import json, sys
sys.stdin.read()
argv = sys.argv[1:]
if "--resume" in argv:
    result_text = json.dumps(
        {"role": "executor", "status": "implemented", "task_id": "RDS-04", "attempt": 1}
    )
else:
    result_text = "- Status: implemented\\n\\n## Files changed\\n- dispatch.py\\n"
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": result_text, "session_id": "sess-wrapped-1",
    "usage": {"input_tokens": 1}, "modelUsage": {}, "total_cost_usd": 0.01,
}))
"""


class ResultTextExtractionTests(unittest.TestCase):
    """Regression for the oxidium-forge PCC-02 failure: a real ``--output-format json`` launch
    wraps the executor's actual prose/envelope text inside a ``result`` field alongside 20+
    telemetry keys. Before RDS-07, ``dispatch_executor`` fed that whole wrapper to
    ``settle_executor_status``, and the strict status envelope failed to parse
    (``unparseable-status-envelope``) even though the agent sent a perfectly well-shaped one."""

    def test_wrapped_cli_output_settles_to_implemented_not_unparseable_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "wrapped_claude.py"
            script.write_text(_WRAPPED_CLAUDE_FAKE, encoding="utf-8")
            adapter = ClaudeAdapter(executable=[sys.executable, str(script)])

            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "implemented")
            self.assertEqual(outcome.settled_status, "implemented")
            # The persisted report is the extracted prose, not the raw CLI wrapper.
            report_text = outcome.artifacts.executor_report.read_text(encoding="utf-8")
            self.assertIn("Status: implemented", report_text)
            self.assertNotIn("total_cost_usd", report_text)
            envelope_text = outcome.artifacts.status_envelope.read_text(encoding="utf-8")
            self.assertNotIn("total_cost_usd", envelope_text)


# --- prompt envelope ---------------------------------------------------------------------------


class PromptEnvelopeTests(unittest.TestCase):
    def test_envelope_makes_report_evidence_runner_owned(self) -> None:
        text = build_executor_envelope(
            _spec(), anchors=_anchors(), role_grant=("read", "write"),
            execution_mode="separate", report_path="reports/RDS-04/launch-1/executor-1.md",
        )
        self.assertNotIn("Report path:", text)
        self.assertNotIn("Write the human Markdown report", text)
        self.assertIn("runner captures your final output", text)

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
        self.assertIn("The listed verification commands are runner-owned evidence.", text)
        self.assertIn("Do not run or report them", text)
        self.assertNotIn("- Run every verification command", text)
        self.assertNotIn("Report path:", text)
        self.assertIn("- Status: implemented | blocked", text)


# --- status settlement through dispatch -------------------------------------------------------


class StatusSettlementTests(unittest.TestCase):
    def test_codex_implemented_final_event_ignores_human_report_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(life, _request(spec), CodexFinalAdapter([{
                "role": "executor", "task_id": spec.id, "attempt": 1,
                "status": "implemented",
            }]))
            self.assertEqual(outcome.status, "implemented")
            self.assertEqual(life.run.task(spec.id).status, "in_progress")
            captured = outcome.artifacts.executor_report.read_text(encoding="utf-8")
            self.assertIn('"status\\": \\"implemented\\"', captured)


class RecoveryEvidenceTests(unittest.TestCase):
    def test_reverse_diff_evidence_is_satisfied_in_the_executor_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "recover.txt"
            target.write_bytes(b"original bytes\n")
            spec = _spec(
                allowed_scope=("recover.txt",),
                runner_evidence="reverse-diff-and-restore",
            )
            life = _running_life(root, spec)
            adapter = ScriptedAdapter()

            dispatch_executor(life, _request(spec), adapter)

            prompt = adapter.calls[0]["prompt"]
            self.assertIn(
                "- Runner evidence: satisfied — reverse-diff-and-restore completed by the runner.",
                prompt,
            )
            self.assertIn(
                "- This evidence is runner-owned; do not write any run artifacts.", prompt)
            self.assertNotIn(str(root), prompt)

    def test_no_runner_evidence_does_not_invent_an_envelope_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = ScriptedAdapter()

            dispatch_executor(life, _request(spec), adapter)

            self.assertNotIn("Runner evidence:", adapter.calls[0]["prompt"])

    def test_runner_captures_and_proves_requested_recovery_evidence_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "recover.txt"
            target.write_bytes(b"original bytes\n")
            spec = _spec(allowed_scope=("recover.txt",), runner_evidence="reverse-diff-and-restore")
            life = _running_life(root, spec)
            outcome = dispatch_executor(life, _request(spec), ScriptedAdapter())

            self.assertEqual(outcome.status, "implemented")
            self.assertTrue(outcome.artifacts.recovery_patch.is_file())
            proof = json.loads(outcome.artifacts.recovery_proof.read_text(encoding="utf-8"))
            self.assertTrue(proof["restored"])
            self.assertEqual(proof["files"][0]["path"], "recover.txt")

    def test_recovery_capture_failure_stops_before_adapter_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec(allowed_scope=(".",), runner_evidence="reverse-diff-and-restore")
            life = _running_life(root, spec)
            adapter = ScriptedAdapter()

            with self.assertRaisesRegex(Exception, "recovery"):
                dispatch_executor(life, _request(spec), adapter)
            self.assertEqual(adapter.calls, [])

    def test_codex_multiple_or_invalid_final_events_are_retryable_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            payload = {"role": "executor", "task_id": spec.id, "attempt": 1,
                       "status": "implemented"}
            outcome = dispatch_executor(life, _request(spec), CodexFinalAdapter([payload, payload]))
            self.assertEqual(outcome.status, "retryable")
            self.assertEqual(life.run.task(spec.id).status, "in_progress")
            self.assertTrue(outcome.artifacts.result_protocol_invalid.is_file())
            self.assertNotIn("executor reported blocked", life.run.task(spec.id).blocker or "")

    def test_codex_contradictory_generated_envelope_retains_both_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            artifacts = launch_artifacts(life.run.run_dir, spec.id, 1)
            conflicting = _envelope("blocked", spec.id)
            artifacts.status_envelope.parent.mkdir(parents=True, exist_ok=True)
            artifacts.status_envelope.write_text(conflicting, encoding="utf-8")
            payload = {
                "role": "executor", "task_id": spec.id, "attempt": 1,
                "status": "implemented",
            }

            outcome = dispatch_executor(life, _request(spec), CodexFinalAdapter([payload]))

            self.assertEqual(outcome.status, "retryable")
            diagnostic = json.loads(
                outcome.artifacts.result_protocol_invalid.read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["envelope_stdout"], conflicting)
            self.assertIn('\\"status\\": \\"implemented\\"', diagnostic["stdout"])

    def test_codex_equivalent_generated_envelope_is_not_a_contradiction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            artifacts = launch_artifacts(life.run.run_dir, spec.id, 1)
            artifacts.status_envelope.parent.mkdir(parents=True, exist_ok=True)
            artifacts.status_envelope.write_text(
                json.dumps({
                    "status": "implemented", "attempt": 1,
                    "task_id": spec.id, "role": "executor",
                }, indent=2),
                encoding="utf-8",
            )

            outcome = dispatch_executor(life, _request(spec), CodexFinalAdapter([{
                "role": "executor", "task_id": spec.id, "attempt": 1,
                "status": "implemented",
            }]))

            self.assertEqual(outcome.status, "implemented")
            self.assertFalse(outcome.artifacts.result_protocol_invalid.exists())

    def test_codex_blocked_final_event_preserves_its_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(life, _request(spec), CodexFinalAdapter([{
                "role": "executor", "task_id": spec.id, "attempt": 1,
                "status": "blocked", "reason": "required service is unavailable",
            }]))
            self.assertEqual(outcome.status, "blocked")
            wait = life.run.task(spec.id).operation_history[-1]
            self.assertEqual(wait["kind"], "wait")
            self.assertEqual(wait["detail"], "required service is unavailable")

    def test_codex_prompt_uses_the_current_repair_attempt(self) -> None:
        spec = _spec()
        prompt = build_executor_envelope(
            spec, anchors=_anchors(), role_grant=("read",), execution_mode="separate",
            report_path="report.md", attempt=2,
        )
        self.assertIn('"attempt":2', prompt)
    def test_fresh_envelope_adapter_receives_the_runner_observed_status(self) -> None:
        class FreshEnvelopeAdapter(ScriptedAdapter):
            requires_fresh_envelope_context = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = FreshEnvelopeAdapter()
            outcome = dispatch_executor(life, _request(spec), adapter)

        self.assertEqual(outcome.status, "implemented")
        self.assertIn(
            "Runner-observed status from the executor report: implemented.",
            adapter.calls[1]["prompt"],
        )

    def test_agreeing_prose_and_envelope_yield_implemented(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(life, _request(spec), ScriptedAdapter())

            self.assertEqual(outcome.status, "implemented")
            self.assertEqual(life.run.task(spec.id).status, "in_progress")
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

    def test_prose_envelope_disagreement_is_retryable_not_a_task_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = ScriptedAdapter(
                prose_status="implemented",
                envelope_text=_envelope("blocked", spec.id, reason="declared check unavailable"),
            )
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "retryable")
            self.assertIsNone(outcome.settled_status)
            self.assertIn("status-envelope-mismatch", outcome.failure or "")
            self.assertEqual(life.run.task(spec.id).status, "in_progress")
            self.assertTrue(outcome.artifacts.launch_failure.exists())

    def test_malformed_status_envelope_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = ScriptedAdapter(envelope_text='{"role": "executor" oops')
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "retryable")
            self.assertIn("unparseable-status-envelope", outcome.failure or "")
            self.assertEqual(life.run.task(spec.id).status, "in_progress")

    def test_executor_reported_blocked_without_a_reason_is_a_retryable_protocol_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = ScriptedAdapter(prose_status="blocked", envelope_status="blocked")
            adapter.requires_blocked_envelope_reason = True
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "retryable")
            self.assertIsNone(outcome.settled_status)
            self.assertTrue(outcome.artifacts.launch_failure.exists())
            self.assertEqual(life.run.task(spec.id).status, "in_progress")
            self.assertIsNone(life.run.task(spec.id).blocker)

    def test_executor_blocked_reason_is_preserved_not_reduced_to_a_generic_string(
        self,
    ) -> None:
        """RLC-01 AC-2: a Claude executor that supplies a non-empty blocked reason on the
        status envelope must have that exact reason preserved into durable operation
        history and the dispatch outcome, not the generic 'executor reported blocked'."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = ScriptedAdapter(
                prose_status="blocked",
                envelope_text=_envelope(
                    "blocked", spec.id, reason="required credential is unavailable"),
            )
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "blocked")
            wait = life.run.task(spec.id).operation_history[-1]
            self.assertEqual(wait["kind"], "wait")
            self.assertEqual(wait["detail"], "required credential is unavailable")

    def test_executor_blocked_with_malformed_reason_is_retryable_not_a_task_block(
        self,
    ) -> None:
        """A blocked envelope that carries an empty/invalid 'reason' is a protocol error —
        it must fail closed as retryable, never invent a reason to block the task on."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            adapter = ScriptedAdapter(
                prose_status="blocked",
                envelope_text=_envelope("blocked", spec.id, reason="   "),
            )
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "retryable")
            self.assertIn("unparseable-status-envelope", outcome.failure or "")
            self.assertEqual(life.run.task(spec.id).status, "in_progress")
            self.assertIsNone(life.run.task(spec.id).blocker)


# --- state authority -------------------------------------------------------------------------


class StateAuthorityTests(unittest.TestCase):
    def test_dispatch_reaches_implemented_blocked_or_retryable(self) -> None:
        for adapter, expected in (
            (ScriptedAdapter(), "implemented"),
            (ScriptedAdapter(
                prose_status="blocked",
                envelope_text=_envelope("blocked", reason="declared check unavailable"),
            ), "blocked"),
            (ScriptedAdapter(launch_exit=1), "retryable"),
        ):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec = _spec()
                life = _running_life(root, spec)
                outcome = dispatch_executor(life, _request(spec), adapter)
                self.assertEqual(outcome.status, expected)
                self.assertIn(outcome.status, {"implemented", "blocked", "retryable"})

    def test_executor_output_leaves_completion_for_independent_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            dispatch_executor(life, _request(spec), ScriptedAdapter())

            self.assertEqual(life.run.task(spec.id).status, "in_progress")
            self.assertIsNone(life.run.task(spec.id).verification["task_verdict"])
            self.assertIsNone(life.run.task(spec.id).verification["test_verdict"])

    def test_dispatch_attributes_the_window_and_fails_closed_without_a_repo(self) -> None:
        # The dispatch fixtures run outside a Git work tree, so attribution has no baseline:
        # it must persist an explicit 'unavailable' packet, never a silent empty one.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec()
            life = _running_life(root, spec)
            outcome = dispatch_executor(life, _request(spec), ScriptedAdapter())

            self.assertEqual(outcome.attribution.state, "unavailable")
            self.assertTrue(outcome.attribution.reason)
            self.assertTrue(outcome.artifacts.implementation_manifest.is_file())
            self.assertTrue(outcome.artifacts.implementation_diff.is_file())
            manifest = json.loads(
                outcome.artifacts.implementation_manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["attribution_state"], "unavailable")
            self.assertEqual(manifest["changed_files"], [])
            self.assertIsNotNone(manifest["reason"])

            implementation = life.run.task(spec.id).execution_evidence["implementation"]
            self.assertEqual(implementation["state"], "unavailable")
            self.assertTrue(
                implementation["manifest"].endswith(
                    "launch-1/implementation-manifest-1.json"))
            self.assertTrue(
                implementation["diff"].endswith("launch-1/implementation-diff-1.md"))


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


def _git(root: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text("storage/\nprompts/\n", encoding="utf-8")
    (root / "src.py").write_text("print('base')\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")


class DispatchAttributionTests(unittest.TestCase):
    def test_tc03_runner_projection_is_durable_and_not_charged_to_allowed_review(self) -> None:
        """TC-03: prior runner projections are protected context, not executor work.

        TC-01/TC-02 and the board are already dirty because the runner projected their
        lifecycle state.  TC-03's executor writes only its declared review artifact.  The
        executor manifest must therefore contain only that review while the prior writes
        remain durably attributed to the runner.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            protected = (
                "docs/kanban.md",
                "docs/plans/tasks/TC-01.md",
                "docs/plans/tasks/TC-02.md",
            )
            for relative in protected:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("runner lifecycle projection\n", encoding="utf-8")
            spec = _spec(allowed_scope=("reviews/TC-03.md",))
            life = _running_life(root, spec)
            life.run.add_task("TC-01")
            life.run.add_task("TC-02")
            life.run.record_runner_projection("TC-01", protected[:2])
            life.run.record_runner_projection("TC-02", (protected[2],))

            def executor_writes_review(request) -> None:  # noqa: ANN001
                review = Path(request.working_root) / "reviews" / "TC-03.md"
                review.parent.mkdir(parents=True, exist_ok=True)
                review.write_text("TC-03 review\n", encoding="utf-8")

            outcome = dispatch_executor(
                life, _request(spec), ScriptedAdapter(on_launch=executor_writes_review))

            self.assertEqual(outcome.status, "implemented")
            self.assertEqual(
                [row["path"] for row in outcome.attribution.changed_files],
                ["reviews/TC-03.md"],
            )
            self.assertEqual(
                [entry["path"] for entry in life.run.task("TC-01").runner_owned_writes],
                list(protected[:2]),
            )
            self.assertEqual(
                [entry["path"] for entry in life.run.task("TC-02").runner_owned_writes],
                [protected[2]],
            )

    def test_executor_change_outside_initial_estimate_is_recorded_for_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            protected = "docs/kanban.md"
            path = root / protected
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("runner lifecycle projection\n", encoding="utf-8")
            spec = _spec(allowed_scope=("reviews/TC-03.md",))
            life = _running_life(root, spec)
            life.run.record_runner_projection(spec.id, (protected,))

            def executor_changes_protected(request) -> None:  # noqa: ANN001
                (Path(request.working_root) / protected).write_text(
                    "executor overwrite\n", encoding="utf-8")

            outcome = dispatch_executor(
                life, _request(spec), ScriptedAdapter(on_launch=executor_changes_protected))

            self.assertEqual(outcome.status, "implemented")
            amendment = life.run.task(spec.id).execution_evidence["implementation"]["scope_amendment"]
            self.assertEqual(amendment["observed_paths"], [protected])
            self.assertEqual(amendment["approval"], "pending-independent-verification")
            self.assertEqual(amendment["original_allowed_scope"], ["reviews/TC-03.md"])
            # The executor's safe expansion is retained in immutable attribution for the
            # independent verifier, but cannot overwrite pre-existing runner-owned bytes in
            # the primary worktree before that review.
            self.assertEqual(path.read_text(encoding="utf-8"), "runner lifecycle projection\n")

    def test_known_delta_captures_the_executor_edit_and_classifies_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            spec = _spec(allowed_scope=("src.py",))
            life = _running_life(root, spec)

            def mutate(request) -> None:  # noqa: ANN001
                workspace = Path(request.working_root)
                (workspace / "src.py").write_text("print('changed by executor')\n", encoding="utf-8")
                (workspace / "stray.txt").write_text("out of scope\n", encoding="utf-8")

            outcome = dispatch_executor(
                life, _request(spec), ScriptedAdapter(on_launch=mutate))

            self.assertEqual(outcome.status, "implemented")
            self.assertEqual(outcome.attribution.state, "known")
            by_path = {row["path"]: row for row in outcome.attribution.changed_files}
            self.assertEqual(by_path["src.py"]["status"], "modified")
            self.assertEqual(by_path["src.py"]["classification"], "in_allowed_scope")
            self.assertTrue(by_path["src.py"]["digest"].startswith("sha256:"))
            self.assertEqual(by_path["stray.txt"]["classification"], "out_of_scope")

            implementation = life.run.task(spec.id).execution_evidence["implementation"]
            self.assertEqual(
                implementation["scope_amendment"]["observed_paths"], ["stray.txt"])
            self.assertEqual((root / "stray.txt").read_text(encoding="utf-8"), "out of scope\n")

            diff = outcome.artifacts.implementation_diff.read_text(encoding="utf-8")
            self.assertIn("# attribution: known", diff)
            self.assertIn("changed by executor", diff)
            self.assertEqual(
                life.run.task(spec.id).changed_files, ["src.py", "stray.txt"])

    def test_preexisting_dirt_is_excluded_but_a_further_edit_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            # Pre-existing, un-committed changes present before the launch window opens.
            (root / "src.py").write_text("print('pre-existing dirt')\n", encoding="utf-8")
            (root / "untouched.py").write_text("# pre-existing new file\n", encoding="utf-8")
            spec = _spec(allowed_scope=("src.py", "untouched.py"))
            life = _running_life(root, spec)

            def mutate(request) -> None:  # noqa: ANN001
                (Path(request.working_root) / "src.py").write_text(
                    "print('executor went further')\n", encoding="utf-8")

            outcome = dispatch_executor(
                life, _request(spec), ScriptedAdapter(on_launch=mutate))

            self.assertEqual(outcome.attribution.state, "known")
            paths = {row["path"] for row in outcome.attribution.changed_files}
            self.assertEqual(paths, {"src.py"})  # untouched.py drops out entirely

    def test_empty_window_is_known_empty_not_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            spec = _spec(allowed_scope=("src.py",))
            life = _running_life(root, spec)
            outcome = dispatch_executor(life, _request(spec), ScriptedAdapter())

            self.assertEqual(outcome.attribution.state, "known-empty")
            self.assertEqual(outcome.attribution.changed_files, [])
            manifest = json.loads(
                outcome.artifacts.implementation_manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["attribution_state"], "known-empty")
            self.assertIn(
                "known-empty",
                outcome.artifacts.implementation_diff.read_text(encoding="utf-8"))

    def test_runner_records_commit_and_tag_created_inside_executor_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            spec = _spec(allowed_scope=("src.py",))
            life = _running_life(root, spec)

            def mutate(request) -> None:  # noqa: ANN001
                workspace = Path(request.working_root)
                (workspace / "src.py").write_text("print('committed by executor')\n", encoding="utf-8")
                _git(workspace, "add", "src.py")
                _git(workspace, "commit", "-qm", "executor commit")
                _git(workspace, "tag", "executor-tag")

            outcome = dispatch_executor(
                life, _request(spec), ScriptedAdapter(on_launch=mutate))

            self.assertEqual(outcome.attribution.state, "known-empty")
            actions = life.run.task(spec.id).execution_evidence["external_actions"]
            self.assertIn("commit", [action["action"] for action in actions])
            self.assertIn("tag", [action["action"] for action in actions])

    def test_runner_owned_run_dir_churn_is_never_attributed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            # Track the run directory so ls-files would surface it if it were not excluded.
            (root / ".gitignore").write_text("prompts/\n", encoding="utf-8")
            spec = _spec(allowed_scope=("src.py",))
            life = _running_life(root, spec)

            def mutate(request) -> None:  # noqa: ANN001
                (life.run.run_dir / "runner-note.txt").write_text("churn\n", encoding="utf-8")

            outcome = dispatch_executor(
                life, _request(spec), ScriptedAdapter(on_launch=mutate))

            self.assertEqual(outcome.attribution.state, "known-empty")

    def test_runner_artifact_directory_is_never_attributed_or_promoted(self) -> None:
        """TAM-01: executor-window artifacts are runner output, not task changes."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            spec = _spec(allowed_scope=("src.py",))
            life = _running_life(root, spec)
            artifact = ".pipeline-artifacts/TAM-01/launch-8/executor-8.md"

            def write_runner_artifact(request) -> None:  # noqa: ANN001
                path = Path(request.working_root) / artifact
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("runner-owned artifact\n", encoding="utf-8")

            outcome = dispatch_executor(
                life, _request(spec), ScriptedAdapter(on_launch=write_runner_artifact))

            self.assertEqual(outcome.attribution.state, "known-empty")
            self.assertEqual(outcome.attribution.changed_files, [])
            self.assertFalse((root / artifact).exists())

    def test_repair_report_is_available_in_the_isolated_executor_workspace(self) -> None:
        """TAM-01 repair findings must be readable at the exact prompt path."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            spec = _spec(allowed_scope=("src.py",))
            life = _running_life(root, spec)
            repair_path = ".pipeline/runs/dispatch/reports/RDS-04/repair-1.md"
            source = root / repair_path
            source.parent.mkdir(parents=True)
            source.write_text(
                "# Repair findings\n\n- Task: RDS-04 — isolated workspace coverage\n"
                "- Revision: 0\n\n- Fix this.\n",
                encoding="utf-8",
            )

            def repair_report_is_readable(request) -> None:  # noqa: ANN001
                visible = Path(request.working_root) / repair_path
                self.assertEqual(visible.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
                self.assertEqual(request.required_input_dirs, (str(visible.parent),))

            outcome = dispatch_executor(
                life,
                _request(spec, repair_report_path=repair_path),
                ScriptedAdapter(on_launch=repair_report_is_readable),
            )

            self.assertEqual(outcome.status, "implemented")
            self.assertFalse((root / repair_path).is_symlink())


class CodexIsolatedWorkspaceCompositionTests(unittest.TestCase):
    """Windows Codex executor-workspace handoff regression (PAC-03 amendment).

    Runner evidence ``pac03-codex-20260914`` recorded ``Access denied`` for the
    disposable Codex workspace on Windows. This exercises the *actual*
    ``dispatch_executor`` -> :class:`CodexAdapter` -> subprocess composition — not a
    hand-built :func:`build_codex_argv` fixture — so the effective routed working
    root, its matching ``--add-dir`` grant, and the runner's own post-window read
    access are all proven together, without weakening sandbox isolation.
    """

    def test_codex_executor_reads_and_writes_its_routed_isolated_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            spec = _spec(allowed_scope=("src.py",))
            life = _running_life(root, spec)

            observed: dict[str, object] = {}

            def fake_runner(argv, *, prompt, cwd, timeout, env):  # noqa: ANN001
                cd_value = argv[argv.index("--cd") + 1]
                add_dirs = [
                    argv[index + 1] for index, token in enumerate(argv) if token == "--add-dir"
                ]
                observed["cd"] = cd_value
                observed["cwd"] = cwd
                observed["add_dirs"] = add_dirs
                # Prove the granted directory is actually writable and readable by the
                # launched process: the exact failure mode ("Access denied") recorded
                # against the disposable Codex workspace on Windows.
                target = Path(cd_value) / "src.py"
                target.write_text("print('written by codex')\n", encoding="utf-8")
                observed["read_back"] = target.read_text(encoding="utf-8")
                payload = {
                    "role": "executor", "status": "implemented",
                    "task_id": spec.id, "attempt": 1,
                }
                events = [
                    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                    json.dumps({
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": json.dumps(payload)},
                    }),
                    json.dumps({"type": "turn.completed"}),
                ]
                return CompletedProcess(0, "\n".join(events), "")

            adapter = CodexAdapter(executable="codex", runner=fake_runner)
            outcome = dispatch_executor(life, _request(spec), adapter)

            self.assertEqual(outcome.status, "implemented")
            # The launched process could read back exactly what it wrote: no
            # Windows sandbox "Access denied" against the routed workspace.
            self.assertEqual(observed["read_back"], "print('written by codex')\n")
            # The `--cd` argv value is exactly the process cwd, and it carries its own
            # `--add-dir` grant (the Windows workspace-write fix).
            self.assertEqual(str(observed["cwd"]), observed["cd"])
            self.assertIn(observed["cd"], observed["add_dirs"])
            # The routed workspace is disposable and isolated from the primary
            # worktree, never the primary worktree itself or a subpath of it.
            self.assertNotEqual(Path(str(observed["cd"])), root)
            self.assertFalse(str(observed["cd"]).startswith(str(root)))
            # The runner can subsequently read the attributed delta: this is the
            # scoped sentinel/attributed-delta half of the handoff, proven through
            # the real attribution and promotion path rather than a bespoke fixture.
            self.assertEqual(
                (root / "src.py").read_text(encoding="utf-8"), "print('written by codex')\n")
            self.assertEqual(outcome.attribution.state, "known")
            self.assertEqual(
                [row["path"] for row in outcome.attribution.changed_files], ["src.py"])
            # The task status remains inside the three-state model throughout.
            self.assertEqual(life.run.task(spec.id).status, "in_progress")


if __name__ == "__main__":
    unittest.main()
