"""PAC-03 — the complete three-state task lifecycle, proven end to end.

One deterministic acceptance matrix, composed entirely of production entry points
(``pipeline_core.execution.execute_run``, ``pipeline_core.state.Run.transition_task``,
``feature_pipeline.infrastructure.board_projection.project_task_state``,
``pipeline_core.dispatch``/``pipeline_core.adapters``) and the installed-package
``fixtures/execution`` plan/scenario fakes already used by ``tests/test_execute_mode.py`` —
never only the isolated state helpers a unit test would use. Every scenario below has a
stable identifier, names its production entry point, its fixture inputs, the expected public
task status, the expected operation-history outcome, the expected Result/projection state,
and the immutable artifact/evidence it asserts against.

At every asserted boundary the *public* task status observed is one of exactly
``to_do`` / ``in_progress`` / ``done`` (never ``blocked`` — AC-1). Richer operation facts
(failed verdicts, lease contention, launch/protocol failures, retry escalation) are asserted
separately, as durable ``operation_history`` entries on an otherwise unfinished task.

Standard library only (plus the already-vendored test fixtures/fakes).
"""

from __future__ import annotations

import json
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline_core.adapters import (
    AdapterError,
    LaunchRequest,
    LaunchResult,
    PUSH_DENY_TOOL,
    build_claude_argv,
)
from pipeline_core.dispatch import _unsafe_scope_amendment_path
from pipeline_core.execution import (
    EXIT_BLOCKED,
    EXIT_OK,
    ExecuteControls,
    ExecutionError,
    execute_run,
    persist_task_contracts,
    _ensure_precondition_contracts_match,
    _next_actionable,
    _pending_reason,
)
from pipeline_core.git_port import GitPort, GitSafetyError
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.state import ACTOR_HUMAN, ACTOR_RUNNER, Run, TransitionError
from pipeline_core.verification import VerifierLaunchers
from feature_pipeline.infrastructure.board_projection import (
    CommandEvidence,
    CompletionEvidence,
    InvalidEvidenceError,
    project_task_state,
)
from feature_pipeline.infrastructure.git.safety import GitPolicyError, parse_read_only_git

from tests.support.fixtures import temp_root, write_file
from tests.test_execute_mode import _controls, _request, _seed_board, _specs
import scenario_adapters as sa  # noqa: F401  (path already injected by test_execute_mode)

#: Every public task status this module may observe. Asserted at every scenario boundary.
_PUBLIC_STATUSES = {"to_do", "in_progress", "done"}


def _assert_public_status(case: unittest.TestCase, status: str) -> None:
    case.assertIn(status, _PUBLIC_STATUSES)


BOARD = """# Kanban Board

## To Do

- [ABC-01: Do the thing](plans/tasks/ABC-01_do-the-thing.md)

## In Progress
"""

TASK = """# ABC-01 - Do the thing

Plan — [example.md](../example.md)

## Status
- [ ] To Do
- [ ] In Progress
- [ ] Done

## Execution Metadata
- Type: python
"""


def _evidence(**overrides: object) -> CompletionEvidence:
    fields: dict[str, object] = dict(
        completed_at="2026-09-14T00:00:00Z",
        run_id="pac03-run",
        resolution="completed",
        repair_count=0,
        gate_count=0,
        task_verdict="PASS",
        test_verdict="PASS",
        commands=(CommandEvidence(cwd=".", command="pytest", exit_code=0),),
        evidence_paths=("docs/acceptance/artifacts/pac03-run.md",),
    )
    fields.update(overrides)
    return CompletionEvidence(**fields)  # type: ignore[arg-type]


# ================================================================================================
# 1. success-completed
# ================================================================================================


class SuccessCompletedTests(unittest.TestCase):
    """id: success-completed.

    Production entry point: :func:`pipeline_core.execution.execute_run`.
    Fixture inputs: ``fixtures/execution/plan.json`` task ``EX-01`` (direct-success),
    ``sa.ScriptedExecutor``/``sa.ScriptedVerifier`` PASS/PASS.
    Expected public status: ``done``. Expected operation-history outcome:
    ``verification``/``succeeded``. Expected Result/projection: the active board removes the
    card and the task file gets exactly one ``## Result`` section.
    """

    def test_independent_pass_pass_produces_done_completed_with_one_result_and_no_card(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            board = _seed_board(root)
            task_path = root / "fixtures/execution/tasks/EX-01_direct-success.md"

            result = execute_run(_request(
                root, _specs(("EX-01",)), executor=sa.ScriptedExecutor(("implemented",)),
                launchers=VerifierLaunchers(
                    task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                controls=ExecuteControls(plan_approved=True), environment={"claude": True},
                board_path=board))

            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.exit_code, EXIT_OK)

            run = Run.load(result.run_dir, root)
            record = run.task("EX-01")
            _assert_public_status(self, record.status)
            self.assertEqual(record.status, "done")
            self.assertEqual(record.resolution, "completed")
            self.assertEqual(record.verification["task_verdict"], "PASS")
            self.assertEqual(record.verification["test_verdict"], "PASS")
            self.assertTrue(
                any(entry["kind"] == "verification" and entry["outcome"] == "succeeded"
                    for entry in record.operation_history), record.operation_history)

            board_text = board.read_text(encoding="utf-8")
            self.assertNotIn("EX-01", board_text)
            task_text = task_path.read_text(encoding="utf-8")
            self.assertIn("- [x] Done", task_text)
            self.assertEqual(task_text.count("## Result"), 1)
            self.assertIn("outcome: **completed**", task_text)


# ================================================================================================
# 2. cancellation-authority
# ================================================================================================


class CancellationAuthorityTests(unittest.TestCase):
    """id: cancellation-authority.

    Production entry points: :meth:`pipeline_core.state.Run.transition_task` (durable
    authority) and :func:`project_task_state` (human-facing projection).
    Expected public status: ``done`` with ``resolution == "cancelled"`` — distinct from
    ``completed`` verified evidence; a missing/invalid reason, or a non-human actor, is
    refused before the transition ever lands.
    """

    def _run(self, root: Path) -> Run:
        prompt = root / "prompt.md"
        prompt.write_text("feature", encoding="utf-8")
        run = Run.create("demo", prompt, None, root / "runs" / "demo", root)
        run.add_task("T-1")
        return run

    def test_valid_human_cancellation_reaches_done_distinct_from_verified_completion(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._run(root)
            run.transition_task("T-1", "in_progress", ACTOR_RUNNER)
            run.transition_task(
                "T-1", "done", ACTOR_HUMAN, note="no longer needed", resolution="cancelled")

            record = run.task("T-1")
            _assert_public_status(self, record.status)
            self.assertEqual(record.status, "done")
            self.assertEqual(record.resolution, "cancelled")
            self.assertEqual(record.resolution_reason, "no longer needed")
            # Cancellation never claims independent verification evidence.
            self.assertIsNone(record.verification.get("task_verdict"))
            self.assertIsNone(record.verification.get("test_verdict"))
            self.assertEqual(run.history[-1]["actor"], ACTOR_HUMAN)

    def test_missing_reason_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            with self.assertRaises(TransitionError) as ctx:
                run.transition_task("T-1", "done", ACTOR_HUMAN, resolution="cancelled")
            self.assertEqual(ctx.exception.code, "missing-cancellation-reason")
            self.assertEqual(run.task("T-1").status, "to_do")

    def test_blank_reason_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            with self.assertRaises(TransitionError) as ctx:
                run.transition_task("T-1", "done", ACTOR_HUMAN, note="   ", resolution="cancelled")
            self.assertEqual(ctx.exception.code, "missing-cancellation-reason")

    def test_only_a_human_may_cancel(self) -> None:
        with TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            with self.assertRaises(TransitionError) as ctx:
                run.transition_task(
                    "T-1", "done", ACTOR_RUNNER, note="not the runner's call",
                    resolution="cancelled")
            self.assertEqual(ctx.exception.code, "unauthorized-transition")
            self.assertEqual(run.task("T-1").status, "to_do")

    def test_cancelled_projection_is_distinct_from_completed_projection(self) -> None:
        """The board/task-file projection itself distinguishes the two Done evidences and
        refuses a cancellation claiming implementation verification (KLC-02)."""
        with temp_root() as root:
            board = write_file(root / "docs/kanban.md", BOARD)
            task = write_file(root / "docs/plans/tasks/ABC-01_do-the-thing.md", TASK)

            project_task_state(
                board_path=board, task_path=task, task_id="ABC-01",
                task_title="Do the thing", state="done",
                evidence=_evidence(
                    resolution="cancelled", resolution_reason="No longer needed.",
                    task_verdict=None, test_verdict=None, commands=()))

            result = task.read_text(encoding="utf-8")
            self.assertIn("- [x] Done", result)
            self.assertIn("outcome: **cancelled**", result)
            self.assertIn("Reason: No longer needed.", result)
            self.assertNotIn("Task verifier verdict:", result)
            self.assertNotIn("Verification commands:", result)

            with self.assertRaisesRegex(
                InvalidEvidenceError, "must not claim implementation verification"
            ):
                _evidence(
                    resolution="cancelled", resolution_reason="No longer needed.",
                    task_verdict=None, test_verdict=None)


# ================================================================================================
# 3. operational-continuation
# ================================================================================================


class OperationalContinuationTests(unittest.TestCase):
    """id: operational-continuation.

    Production entry point: :func:`execute_run` (plus :class:`~pipeline_core.git_port.GitPort`
    for the lease/durable-operation shape reused elsewhere in this module).
    Every failure mode below (executor launch failure, task-verifier FAIL, test-verifier FAIL,
    an external BLOCKED verdict, lease contention, an invalid Codex result protocol, and
    advisory repair-budget escalation) is asserted to leave the task at public status
    ``in_progress`` — never ``blocked`` — with a specific ``operation_history`` entry, and a
    later permitted resume/retry is proven to still reach ``done`` without any unblock
    transition.
    """

    def _run_once(self, root: Path, *, executor, task, test, controls):
        return execute_run(_request(
            root, _specs(("EX-01",)), executor=executor,
            launchers=VerifierLaunchers(task=task, test=test),
            controls=controls, environment={"claude": True}))

    def test_a_range_of_operation_outcomes_leave_the_task_unfinished_never_blocked(self) -> None:
        cases = (
            ("executor-launch-failure",
             sa.ScriptedExecutor(("launch-fail",)), sa.ScriptedVerifier(("PASS",)),
             sa.ScriptedVerifier(("PASS",)), ExecuteControls(plan_approved=True),
             "executor", "failed"),
            ("task-verifier-fail",
             sa.ScriptedExecutor(("implemented",)), sa.ScriptedVerifier(("FAIL",)),
             sa.ScriptedVerifier(("PASS",)),
             ExecuteControls(plan_approved=True, max_repair_attempts=0),
             "verification", "failed"),
            ("test-verifier-fail",
             sa.ScriptedExecutor(("implemented",)), sa.ScriptedVerifier(("PASS",)),
             sa.ScriptedVerifier(("FAIL",)),
             ExecuteControls(plan_approved=True, max_repair_attempts=0),
             "verification", "failed"),
            ("external-blocked-verdict",
             sa.ScriptedExecutor(("implemented",)), sa.ScriptedVerifier(("BLOCKED",)),
             sa.ScriptedVerifier(("PASS",)), ExecuteControls(plan_approved=True),
             "verification", "blocked"),
            ("malformed-verdict-envelope-invalid-protocol",
             sa.ScriptedExecutor(("implemented",)), sa.ScriptedVerifier(("PASS",)),
             sa.ScriptedVerifier(("PASS",), malformed_envelope=True),
             ExecuteControls(plan_approved=True), None, None),
        )
        for name, executor, task_v, test_v, controls, kind, outcome in cases:
            with self.subTest(scenario=name), TemporaryDirectory() as directory:
                root = Path(directory)
                result = self._run_once(root, executor=executor, task=task_v, test=test_v,
                                        controls=controls)
                self.assertIn(result.status, {"blocked", "retryable"}, name)
                self.assertNotEqual(result.exit_code, EXIT_OK, name)
                run = Run.load(result.run_dir, root)
                record = run.task("EX-01")
                _assert_public_status(self, record.status)
                self.assertEqual(record.status, "in_progress", name)
                self.assertNotIn(
                    "unblock", " ".join(entry["outcome"] for entry in record.operation_history))
                if kind is not None:
                    self.assertTrue(
                        any(entry["kind"] == kind and entry["outcome"] == outcome
                            for entry in record.operation_history),
                        (name, record.operation_history))

    def test_a_live_foreign_task_lease_is_a_durable_operation_not_a_terminal_status(self) -> None:
        from pipeline_core.concurrency import task_lock_path
        import os as _os

        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock = task_lock_path(root, "EX-01")
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(json.dumps({
                "run_id": "other-run", "pid": _os.getpid(), "task_id": "EX-01",
                "started_at": "2026-09-01T00:00:00Z"}), encoding="utf-8")

            result = self._run_once(
                root, executor=sa.ScriptedExecutor(("implemented",)),
                task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",)),
                controls=ExecuteControls(plan_approved=True))

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.exit_code, EXIT_BLOCKED)
            run = Run.load(result.run_dir, root)
            record = run.task("EX-01")
            _assert_public_status(self, record.status)
            self.assertEqual(record.status, "to_do")
            self.assertTrue(
                any(entry.get("kind") == "lease" and entry.get("outcome") == "blocked"
                    for entry in record.operation_history), record.operation_history)

    def test_invalid_codex_result_protocol_permits_a_later_resume_to_reach_done(self) -> None:
        """A malformed Codex final-result payload is an ``invalid-result-protocol`` operation
        outcome (``retryable``), never a terminal task status; a later ``--resume`` with a
        healthy launch reaches ``done`` without an unblock transition."""

        class CodexSequenceExecutor:
            name = "codex"

            def __init__(self) -> None:
                self.launches = 0

            def launch(self, request):  # noqa: ANN001 - deterministic protocol fixture
                payloads = (
                    [{"not": "a final result"}],
                    [{"role": "executor", "task_id": request.task_id, "attempt": 1,
                      "status": "implemented"}],
                )
                current = payloads[self.launches]
                self.launches += 1
                Path(request.report_path).parent.mkdir(parents=True, exist_ok=True)
                Path(request.report_path).write_text("# Human report\n", encoding="utf-8")
                events = [json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": json.dumps(payload)}}) for payload in current]
                events.append(json.dumps({"type": "turn.completed"}))
                return LaunchResult(0, "# Human report\n", "", "thread-1", "\n".join(events))

        with TemporaryDirectory() as directory:
            root = Path(directory)
            executor = CodexSequenceExecutor()
            first = execute_run(_request(
                root, _specs(("EX-01",)), executor=executor,
                launchers=VerifierLaunchers(
                    task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                controls=ExecuteControls(plan_approved=True, adapter="codex", adapter_explicit=True),
                environment={"codex": True}))

            self.assertEqual(first.status, "retryable")
            run = Run.load(first.run_dir, root)
            record = run.task("EX-01")
            _assert_public_status(self, record.status)
            self.assertEqual(record.status, "in_progress")
            self.assertTrue(
                (first.run_dir / "reports" / "EX-01" / "launch-1" /
                 "result-protocol-invalid-1.json").is_file())

            resumed = execute_run(_request(
                root, _specs(("EX-01",)), executor=executor,
                launchers=VerifierLaunchers(
                    task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                controls=ExecuteControls(
                    plan_approved=True, resume=True, adapter="codex", adapter_explicit=True),
                environment={"codex": True}))

            self.assertEqual(resumed.status, "ok")
            run = Run.load(resumed.run_dir, root)
            record = run.task("EX-01")
            _assert_public_status(self, record.status)
            self.assertEqual(record.status, "done")
            self.assertNotIn(
                "unblock", " ".join(entry["outcome"] for entry in record.operation_history))

    def test_repair_budget_escalation_stays_unfinished_and_a_later_gate_still_completes(self) -> None:
        """The advisory repair threshold is escalation evidence on an unfinished task, never a
        terminal status: exhausting it (max_repair_attempts=0) records ``escalated``; the same
        FAIL-then-PASS mechanism, given one repair attempt, reaches ``done`` without any
        unblock transition — the budget bounds retries, it never blocks the task."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            exhausted = self._run_once(
                root, executor=sa.ScriptedExecutor(("implemented",)),
                task=sa.ScriptedVerifier(("FAIL",)), test=sa.ScriptedVerifier(("PASS",)),
                controls=ExecuteControls(plan_approved=True, max_repair_attempts=0))
            self.assertEqual(exhausted.exit_code, EXIT_BLOCKED)
            record = Run.load(exhausted.run_dir, root).task("EX-01")
            _assert_public_status(self, record.status)
            self.assertEqual(record.status, "in_progress")
            self.assertIn("escalated", [entry["outcome"] for entry in record.operation_history])

        with TemporaryDirectory() as directory:
            root = Path(directory)
            continued = self._run_once(
                root, executor=sa.ScriptedExecutor(("implemented", "implemented")),
                task=sa.ScriptedVerifier(("FAIL", "PASS")), test=sa.ScriptedVerifier(("PASS", "PASS")),
                controls=ExecuteControls(plan_approved=True))
            self.assertTrue(continued.ok, continued.message)
            record = Run.load(continued.run_dir, root).task("EX-01")
            _assert_public_status(self, record.status)
            self.assertEqual(record.status, "done")
            self.assertNotIn(
                "unblock", " ".join(entry["outcome"] for entry in record.operation_history))


# ================================================================================================
# 4. dependency-waiting
# ================================================================================================


class DependencyWaitingTests(unittest.TestCase):
    """id: dependency-waiting.

    Production entry points: :func:`execute_run` end to end, and the scheduler's own
    :func:`pipeline_core.execution._next_actionable` / ``_pending_reason`` helpers for the
    focused durable-status assertion.
    Expected public status: the dependent stays ``to_do`` (never ``blocked``/terminal) while
    its dependency is incomplete, and becomes eligible once the dependency reaches an
    eligible completion (``completed`` — not ``cancelled``).
    """

    def test_unfinished_dependency_suppresses_dispatch_without_terminalizing_dependents(self) -> None:
        scenario = sa.SCENARIOS["dependency-suppression"]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executor = scenario.executor()
            result = execute_run(_request(
                root, _specs(scenario.task_ids), executor=executor,
                launchers=VerifierLaunchers(
                    task=scenario.task_verifier(), test=scenario.test_verifier()),
                controls=_controls(scenario), environment={"claude": True}))

            self.assertEqual(result.exit_code, EXIT_BLOCKED)
            self.assertEqual({call["task_id"] for call in executor.calls}, {"EX-01"})
            run = Run.load(result.run_dir, root)
            for task_id in ("EX-01", "EX-02", "EX-03"):
                _assert_public_status(self, run.task(task_id).status)
            self.assertEqual(run.task("EX-01").status, "in_progress")
            self.assertEqual(run.task("EX-02").status, "to_do")
            self.assertEqual(run.task("EX-03").status, "to_do")
            self.assertIsNone(run.task("EX-02").blocker)
            self.assertIsNone(run.task("EX-03").blocker)

    def test_deterministic_continuation_once_the_dependency_has_eligible_completion(self) -> None:
        scenario = sa.SCENARIOS["dependency-ordering"]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = execute_run(_request(
                root, _specs(scenario.task_ids), executor=scenario.executor(),
                launchers=VerifierLaunchers(
                    task=scenario.task_verifier(), test=scenario.test_verifier()),
                controls=_controls(scenario), environment={"claude": True}))

            self.assertTrue(result.ok, result.message)
            run = Run.load(result.run_dir, root)
            self.assertEqual(run.task("EX-01").status, "done")
            self.assertEqual(run.task("EX-01").resolution, "completed")
            self.assertEqual(run.task("EX-02").status, "done")

    def test_a_cancelled_dependency_keeps_the_dependent_waiting_not_dispatched(self) -> None:
        """Cancelled is not verified: a dependent may never be freed by a cancelled
        dependency, only by ``completed`` resolution evidence (ROC-02/PAC-03)."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = Run.create("demo", root / "prompt.md", None, root / "runs" / "demo", root)
            life = RunLifecycle(run)
            run.add_task("A")
            run.add_task("B", depends_on=["A"])
            run.transition_task("A", "in_progress")
            run.transition_task("A", "done", actor=ACTOR_HUMAN, resolution="cancelled",
                                note="no longer needed")

            self.assertIsNone(_next_actionable(life, ["A", "B"], ["A", "B"]))
            _assert_public_status(self, run.task("B").status)
            self.assertEqual(run.task("B").status, "to_do")
            self.assertIn("B dependency-not-satisfied: A", _pending_reason(life, ["B"]))

    def test_a_completed_dependency_frees_the_dependent_for_dispatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = Run.create("demo", root / "prompt.md", None, root / "runs" / "demo", root)
            life = RunLifecycle(run)
            run.add_task("A")
            run.add_task("B", depends_on=["A"])
            run.transition_task("A", "in_progress")
            self.assertEqual(run.record_verdicts("A", "PASS", "PASS"), "done")
            self.assertEqual(run.task("A").resolution, "completed")

            self.assertEqual(_next_actionable(life, ["A", "B"], ["A", "B"]), "B")


# ================================================================================================
# 5. amendment-and-attribution
# ================================================================================================


class AmendmentAndAttributionTests(unittest.TestCase):
    """id: amendment-and-attribution.

    Production entry point: :func:`execute_run` (approved amendment path) and
    :func:`pipeline_core.execution._ensure_precondition_contracts_match` (drift refusal).
    Expected public status: ``done`` with a durable ``scope_amendment`` observation on the
    preserved prior evidence; an unapproved contract drift is refused before dispatch.
    """

    def test_approved_scope_amendment_preserves_evidence_and_records_full_attribution(self) -> None:
        class ScopeAmendmentExecutor(sa.ScriptedExecutor):
            def launch(self, request):  # noqa: ANN001 - deterministic fixture
                if not (request.no_tools or request.resume_session_id):
                    amended = (
                        Path(request.working_root)
                        / "fixtures/execution/tasks/EX-01_direct-success.md")
                    amended.write_text(
                        amended.read_text(encoding="utf-8")
                        + "\n## Repair Scope Amendment\n\nRequired to repair the failed "
                        "verification.\n", encoding="utf-8")
                return super().launch(request)

        class AmendmentReviewVerifier(sa.ScriptedVerifier):
            def __init__(self, verdicts, *, role: str) -> None:
                super().__init__(verdicts)
                self.role = role
                self.amendment_reports: list[str] = []

            def launch(self, request):  # noqa: ANN001 - deterministic fixture
                result = super().launch(request)
                if not request.resume_session_id:
                    if "Amendment-justification finding:" not in request.prompt:
                        raise AssertionError(
                            "verifier did not receive the review requirement")
                    report = (
                        f"# {self.role}\n\n- Verdict: {self._verdict()}\n\n"
                        "- Amendment-justification finding: accepted — a reviewable repair "
                        "artifact for the failed verification.\n")
                    self.amendment_reports.append(report)
                    Path(request.report_path).write_text(report, encoding="utf-8")
                return result

        with TemporaryDirectory() as directory:
            root = Path(directory)
            board = _seed_board(root)
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            subprocess.run(("git", "add", "."), cwd=root, check=True)
            subprocess.run(
                ("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture baseline"), cwd=root, check=True)

            task_verifier = AmendmentReviewVerifier(("FAIL", "PASS"), role="task_verifier")
            test_verifier = AmendmentReviewVerifier(("PASS", "PASS"), role="test_verifier")
            result = execute_run(_request(
                root, _specs(("EX-01",)),
                executor=ScopeAmendmentExecutor(("implemented", "implemented")),
                launchers=VerifierLaunchers(task=task_verifier, test=test_verifier),
                controls=ExecuteControls(plan_approved=True), environment={"claude": True},
                board_path=board))

            self.assertTrue(result.ok, result.message)
            run = Run.load(result.run_dir, root)
            record = run.task("EX-01")
            _assert_public_status(self, record.status)
            self.assertEqual(record.status, "done")
            amendment = record.execution_evidence["implementation"]["scope_amendment"]
            self.assertTrue(amendment["present"])
            self.assertIn(
                "fixtures/execution/tasks/EX-01_direct-success.md", amendment["observed_paths"])
            self.assertEqual(
                amendment["rationale"],
                "executor-owned paths outside the initial estimate require independent "
                "amendment-justification review")
            # Prior operation evidence (the failed first gate) is preserved, not erased.
            self.assertIn("failed", [entry["outcome"] for entry in record.operation_history])
            self.assertNotIn(
                "unblock", " ".join(entry["outcome"] for entry in record.operation_history))
            # Both independent verifiers reviewed the amendment (fresh verification epoch).
            reports = task_verifier.amendment_reports + test_verifier.amendment_reports
            self.assertEqual(len(reports), 4)
            for report in reports:
                self.assertIn("Amendment-justification finding: accepted", report)

    def test_unapproved_contract_drift_is_refused_before_dispatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            specs = _specs(("EX-01",))
            run = Run.create("demo", root / "prompt.md", None, root / "runs" / "demo", root)
            run.add_task("EX-01")
            persist_task_contracts(run, specs)

            drifted = (replace(
                specs[0], allowed_scope=specs[0].allowed_scope + ("unreviewed/new-path.py",)),)
            with self.assertRaises(ExecutionError) as ctx:
                _ensure_precondition_contracts_match(run, drifted)
            self.assertEqual(ctx.exception.code, "task-contract-mismatch")
            _assert_public_status(self, run.task("EX-01").status)
            self.assertEqual(run.task("EX-01").status, "to_do")


# ================================================================================================
# 6. safety-refusals
# ================================================================================================


class SafetyRefusalsTests(unittest.TestCase):
    """id: safety-refusals.

    Production entry points: :func:`pipeline_core.dispatch._unsafe_scope_amendment_path`,
    :func:`execute_run` (secret-exposure composition), :func:`build_claude_argv`, and
    :class:`~pipeline_core.git_port.GitPort` / ``parse_read_only_git``. Every refusal is
    mechanical and precedes any verifier launch; none manufactures verified completion and
    none leaves a terminal/blocked task status.
    """

    def test_unsafe_path_escape_is_rejected(self) -> None:
        self.assertTrue(_unsafe_scope_amendment_path("../outside-worktree.txt"))
        self.assertTrue(_unsafe_scope_amendment_path("/etc/passwd"))
        self.assertTrue(_unsafe_scope_amendment_path("C:/Users/me/secret.txt"))
        self.assertFalse(_unsafe_scope_amendment_path("reviews/TC-03.md"))

    def test_secret_exposure_is_refused_without_a_verifier_launch(self) -> None:
        class UnsafeScopeExecutor(sa.ScriptedExecutor):
            def launch(self, request):  # noqa: ANN001 - deterministic fixture
                if not (request.no_tools or request.resume_session_id):
                    unsafe = Path(request.working_root) / "fixtures/execution/work/secrets.txt"
                    unsafe.parent.mkdir(parents=True, exist_ok=True)
                    unsafe.write_text("token=do-not-leak-this-secret-value", encoding="utf-8")
                return super().launch(request)

        class UnreachedVerifier(sa.ScriptedVerifier):
            def launch(self, request):  # noqa: ANN001 - deterministic fixture
                raise AssertionError("no verifier may launch over an unsafe executor path")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            (root / "fixtures" / "execution" / "work").mkdir(parents=True, exist_ok=True)
            (root / ".gitkeep").write_text("", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=root, check=True)
            subprocess.run(
                ("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture baseline"), cwd=root, check=True)

            result = execute_run(_request(
                root, _specs(("EX-01",)), executor=UnsafeScopeExecutor(("implemented",)),
                launchers=VerifierLaunchers(
                    task=UnreachedVerifier(("PASS",)), test=UnreachedVerifier(("PASS",))),
                controls=ExecuteControls(plan_approved=True), environment={"claude": True}))

            self.assertEqual(result.status, "blocked")
            self.assertIn("scope-safety-violation", result.message)
            self.assertIn("secrets.txt", result.message)

            run = Run.load(result.run_dir, root)
            record = run.task("EX-01")
            _assert_public_status(self, record.status)
            self.assertEqual(record.status, "in_progress")
            self.assertNotIn(
                "unblock", " ".join(entry["outcome"] for entry in record.operation_history))

    def test_push_is_structurally_denied_regardless_of_a_pre_approval_attempt(self) -> None:
        with self.assertRaises(AdapterError) as ctx:
            build_claude_argv(LaunchRequest(
                role="executor", task_id="EX-01", prompt="x", report_path=Path("r.md"),
                role_grant=("read", "write"), tools=("Bash",),
                allowed_tools=("Bash(git push origin main)",)))
        self.assertEqual(ctx.exception.code, "push-denied")

        # Every ordinary write launch always carries the structural push denial too.
        argv = build_claude_argv(LaunchRequest(
            role="executor", task_id="EX-01", prompt="x", report_path=Path("r.md"),
            role_grant=("read", "write"), tools=("Bash",)))
        self.assertIn(PUSH_DENY_TOOL, argv[argv.index("--disallowed-tools") + 1])

    def test_unapproved_destructive_git_action_is_refused(self) -> None:
        with self.assertRaises(GitPolicyError):
            parse_read_only_git(("reset", "--hard"))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            with self.assertRaises(GitSafetyError):
                GitPort(root).run(("reset", "--hard"))
            with self.assertRaises(GitSafetyError):
                GitPort(root).run(("push", "origin", "main"))


# ================================================================================================
# 7. projection-idempotence
# ================================================================================================


class ProjectionIdempotenceTests(unittest.TestCase):
    """id: projection-idempotence.

    Production entry point: :func:`execute_run` with ``resume=True`` over a durable ``done``
    run whose Markdown projection was never written (the crash-between-transition-and-
    projection shape). Expected: the resume repairs the stale board/task-file projection from
    durable evidence alone, without redispatching the completed executor/verifiers, without
    replacing prior report-evidence bytes, and without appending a second ``## Result``.
    """

    def test_repeated_resume_repairs_a_stale_projection_without_redispatch_or_duplicate_result(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            board = _seed_board(root)
            task_path = root / "fixtures/execution/tasks/EX-01_direct-success.md"

            first = execute_run(_request(
                root, _specs(("EX-01",)), executor=sa.ScriptedExecutor(("implemented",)),
                launchers=VerifierLaunchers(
                    task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                controls=ExecuteControls(plan_approved=True), environment={"claude": True}))
            self.assertTrue(first.ok, first.message)
            run = Run.load(first.run_dir, root)
            self.assertEqual(run.task("EX-01").status, "done")
            reports_dir = first.run_dir / "reports"
            source_report_bytes = {
                path.relative_to(reports_dir): path.read_bytes()
                for path in sorted(reports_dir.rglob("*")) if path.is_file()
            } if reports_dir.is_dir() else {}

            # Stale: durable done, but the board/task-file projection was never attempted.
            self.assertIn("EX-01", board.read_text(encoding="utf-8"))
            self.assertIn("- [ ] Done", task_path.read_text(encoding="utf-8"))

            for attempt in range(2):  # repeated resume/reconciliation
                executor = sa.ScriptedExecutor(("implemented",))
                task_verifier = sa.ScriptedVerifier(("PASS",))
                test_verifier = sa.ScriptedVerifier(("PASS",))
                resumed = execute_run(_request(
                    root, _specs(("EX-01",)), executor=executor,
                    launchers=VerifierLaunchers(task=task_verifier, test=test_verifier),
                    controls=ExecuteControls(plan_approved=True, resume=True),
                    environment={"claude": True}, board_path=board))

                self.assertTrue(resumed.ok, resumed.message)
                self.assertEqual(executor.launches, 0, f"attempt {attempt}: redispatched")
                self.assertEqual(task_verifier.calls, [], f"attempt {attempt}: redispatched")
                self.assertEqual(test_verifier.calls, [], f"attempt {attempt}: redispatched")

                run = Run.load(resumed.run_dir, root)
                record = run.task("EX-01")
                _assert_public_status(self, record.status)
                self.assertEqual(record.status, "done")

                board_text = board.read_text(encoding="utf-8")
                self.assertNotIn("EX-01", board_text)
                task_text = task_path.read_text(encoding="utf-8")
                self.assertIn("- [x] Done", task_text)
                self.assertEqual(task_text.count("## Result"), 1, f"attempt {attempt}")

                resumed_report_bytes = {
                    path.relative_to(reports_dir): path.read_bytes()
                    for path in sorted(reports_dir.rglob("*")) if path.is_file()
                } if reports_dir.is_dir() else {}
                self.assertEqual(
                    resumed_report_bytes, source_report_bytes, f"attempt {attempt}")


if __name__ == "__main__":
    unittest.main()
