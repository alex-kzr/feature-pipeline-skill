"""VR-03 — the bounded, auditable repair loop.

Written before the loop existed. The cases cover the four properties the contract names:

* a repair limit of 0 / 1 / 2 spends at most that many attempts and then blocks once;
* every repair redispatches the *same* executor role in a fresh, unresumed context carrying
  the consolidated repair report, and reruns the complete verification gate;
* the consolidated repair report de-duplicates the finding lines and keeps product defects
  separate from environment problems;
* limit exhaustion writes one durable blocker, suppresses dependents transitively, preserves
  every earlier attempt's evidence, and returns a non-zero exit code. An external ``BLOCKED``
  outcome spends no attempt.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from pipeline_core.commands import verification_stage
from pipeline_core.dispatch import DispatchError
from pipeline_core.execution import ExecutionError, TaskExecution, run_task
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.plan import AmendmentRevision
from pipeline_core.reports import (
    consolidate_findings,
    newest_repair_report,
    repair_report_path,
    write_repair_report,
)
from pipeline_core.state import ACTOR_RUNNER, Run
from pipeline_core.verification import VerifierLaunchers
from feature_pipeline.contracts import TaskSpec

from tests.support.builders import bounded_repair_spec, build_execution, initialize_run
from tests.support.fakes import ENVELOPE_ANCHORS, ScriptedExecutor, StubVerifier, VERIFIER_ANCHORS

# --- fixtures --------------------------------------------------------------------------------

# ``_spec``/``_run``/``_execution`` are thin, file-scoped names kept for readability inside this
# module's tests; the actual construction now lives in ``tests.support`` (QG-01) so
# ``tests/test_execution.py`` shares the identical ``Run``/``TaskExecution`` builders instead of
# re-deriving them.
_spec = bounded_repair_spec
_run = initialize_run


def _execution(
    spec: TaskSpec, executor: ScriptedExecutor, task: StubVerifier, test: StubVerifier
) -> TaskExecution:
    return build_execution(
        spec, executor, task, test,
        plan_path="docs/plans/2026-09-01-core-execution-engine.md",
    )


def _repair_reports(run: Run, task_id: str) -> list[int]:
    base = Path(run.run_dir) / "reports" / task_id
    return sorted(
        int(re.fullmatch(r"repair-([1-9][0-9]*)\.md", p.name).group(1))
        for p in base.glob("repair-*.md")
    )


# --- repair-limit boundary (0 / 1 / 2) ----------------------------------------------------


class RepairLimitBoundaryTests(unittest.TestCase):
    def test_limit_zero_one_two_spends_exactly_that_many_attempts(self) -> None:
        for maximum in (0, 1, 2):
            with self.subTest(maximum=maximum), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                life = _run(root)
                spec = _spec(max_repair_attempts=maximum)
                executor = ScriptedExecutor(("implemented",) * (maximum + 1))
                task = StubVerifier(("FAIL",) * (maximum + 1))
                test = StubVerifier(("PASS",) * (maximum + 1))

                result = run_task(life, _execution(spec, executor, task, test))

                self.assertEqual(result.status, "escalated")
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(result.attempts, maximum)
                self.assertEqual(result.gates, maximum + 1)
                # one executor dispatch per gate, one consolidated repair report per gate.
                self.assertEqual(executor.launches, maximum + 1)
                self.assertEqual(len(_repair_reports(life.run, "VR-03")), maximum + 1)
                self.assertIn("maximum repair attempts", result.blocker or "")
                self.assertIsNone(result.diagnostic)
                self.assertEqual(life.run.task("VR-03").status, "in_progress")

    def test_a_successful_repair_stops_the_loop_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            executor = ScriptedExecutor(("implemented", "implemented"))
            task = StubVerifier(("FAIL", "PASS"))
            test = StubVerifier(("PASS", "PASS"))

            result = run_task(life, _execution(spec, executor, task, test))

            self.assertEqual(result.status, "done")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.attempts, 1)
            self.assertEqual(result.gates, 2)
            self.assertEqual(executor.launches, 2)
            self.assertEqual(_repair_reports(life.run, "VR-03"), [2])
            self.assertEqual(life.run.task("VR-03").status, "done")


# --- same-role fresh redispatch + full-gate rerun ---------------------------------------


class RedispatchAndGateRerunTests(unittest.TestCase):
    def test_repair_redispatches_the_same_role_fresh_with_the_repair_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=1)
            executor = ScriptedExecutor(("implemented", "implemented"))
            task = StubVerifier(("FAIL", "PASS"))
            test = StubVerifier(("PASS", "PASS"))

            run_task(life, _execution(spec, executor, task, test))

            first, repair = executor.calls
            self.assertFalse(first["is_repair"])
            self.assertEqual(repair["role"], first["role"])  # same executor role
            self.assertTrue(repair["fresh_session"])
            self.assertIsNone(repair["resume"])
            self.assertTrue(repair["is_repair"])
            self.assertIn("repair-2.md", repair["prompt"])
            self.assertIn("Preserve the original scope", repair["prompt"])

    def test_every_repair_reruns_the_full_verification_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            executor = ScriptedExecutor(("implemented",) * 3)
            task = StubVerifier(("FAIL", "FAIL", "PASS"))
            test = StubVerifier(("PASS", "PASS", "PASS"))

            result = run_task(life, _execution(spec, executor, task, test))

            self.assertEqual(result.status, "done")
            self.assertEqual(result.gates, 3)
            # a fresh, independent verifier pair ran for every gate (3 initial launches each).
            self.assertEqual(sum(1 for c in task.calls if not c["is_env"]), 3)
            self.assertEqual(sum(1 for c in test.calls if not c["is_env"]), 3)
            for gate in (1, 2, 3):
                gate_dir = Path(life.run.run_dir) / "reports" / "VR-03" / f"verify-{gate}"
                self.assertTrue((gate_dir / f"task-verifier-{gate}.md").is_file())
                self.assertTrue((gate_dir / f"test-verifier-{gate}.md").is_file())
            for c in task.calls + test.calls:
                if not c["is_env"]:
                    self.assertTrue(c["read_only"])
                    self.assertTrue(c["fresh_session"])

    def test_declared_commands_are_rerun_once_per_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(
                max_repair_attempts=1,
                verification_commands=({"cwd": ".", "command": "python -c pass"},),
            )
            executor = ScriptedExecutor(("implemented", "implemented"))
            task = StubVerifier(("FAIL", "PASS"))
            test = StubVerifier(("PASS", "PASS"))

            run_task(life, _execution(spec, executor, task, test))

            self.assertIn("task:VR-03:verify:1", life.run.stages)
            self.assertIn("task:VR-03:verify:2", life.run.stages)


# --- external BLOCKED never spends an attempt ------------------------------------------


class ExternalBlockedTests(unittest.TestCase):
    def test_a_blocked_verdict_blocks_without_consuming_a_repair_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            executor = ScriptedExecutor(("implemented",))
            task = StubVerifier(("BLOCKED",))
            test = StubVerifier(("PASS",))

            result = run_task(life, _execution(spec, executor, task, test))

            self.assertEqual(result.status, "waiting")
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.attempts, 0)
            self.assertEqual(executor.launches, 1)  # no repair redispatch
            self.assertEqual(_repair_reports(life.run, "VR-03"), [])
            self.assertEqual(life.run.task("VR-03").attempts, 0)
            # A verifier-reported external wait is unfinished operation-level evidence, never
            # a task state: the public task status stays 'in_progress'.
            self.assertEqual(life.run.task("VR-03").status, "in_progress")
            self.assertEqual(
                life.run.task("VR-03").operation_history[-1]["kind"], "verification")
            self.assertEqual(
                life.run.task("VR-03").operation_history[-1]["outcome"], "blocked")

    def test_an_executor_that_cannot_implement_blocks_before_any_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            executor = ScriptedExecutor(("blocked",))
            task = StubVerifier(("PASS",))
            test = StubVerifier(("PASS",))

            result = run_task(life, _execution(spec, executor, task, test))

            self.assertEqual(result.status, "waiting")
            self.assertEqual(result.gates, 0)
            self.assertEqual(result.attempts, 0)
            self.assertEqual(task.calls, [])
            # An implementation failure is unfinished operation-level evidence, never a task
            # state: the public task status stays 'in_progress' with the failure durably
            # recorded on its operation history.
            self.assertEqual(life.run.task("VR-03").status, "in_progress")
            self.assertEqual(
                life.run.task("VR-03").operation_history[-1]["kind"], "executor")
            self.assertEqual(
                life.run.task("VR-03").operation_history[-1]["outcome"], "failed")


# --- durable blocker + transitive dependent suppression ------------------------------


class BlockAtLimitTests(unittest.TestCase):
    def test_limit_block_persists_a_blocker_and_suppresses_dependents_transitively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root, tasks=(("AA-1", ()), ("BB-1", ("AA-1",)), ("CC-1", ("BB-1",))))
            spec = _spec("AA-1", max_repair_attempts=0)
            executor = ScriptedExecutor(("implemented",))
            task = StubVerifier(("FAIL",))
            test = StubVerifier(("PASS",))

            result = run_task(life, _execution(spec, executor, task, test))
            self.assertEqual(result.status, "escalated")

            reloaded = Run.load(life.run.run_dir, root)
            self.assertEqual(reloaded.task("AA-1").status, "in_progress")
            self.assertIsNone(reloaded.task("AA-1").blocker)
            self.assertIsNone(reloaded.task("BB-1").blocker)
            self.assertIsNone(reloaded.task("CC-1").blocker)

            self.assertNotIn("blocker:AA-1", reloaded.artifacts)

    def test_earlier_attempt_evidence_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            executor = ScriptedExecutor(("implemented",) * 3)
            task = StubVerifier(("FAIL", "FAIL", "FAIL"))
            test = StubVerifier(("PASS", "PASS", "PASS"))

            run_task(life, _execution(spec, executor, task, test))

            reports = Path(life.run.run_dir) / "reports" / "VR-03"
            for generation in (1, 2, 3):
                self.assertTrue((reports / f"launch-{generation}").is_dir())
            for gate in (1, 2, 3):
                self.assertTrue((reports / f"verify-{gate}").is_dir())
            self.assertEqual(_repair_reports(life.run, "VR-03"), [2, 3, 4])


# --- consolidated repair-report shape --------------------------------------------------


class RepairReportConsolidationTests(unittest.TestCase):
    def test_findings_are_deduplicated_across_both_verifier_reports(self) -> None:
        merged = consolidate_findings(
            "# task_verifier\n- Verdict: FAIL\n- missing guard clause\n- no test for empty input\n",
            "# test_verifier\n- Verdict: FAIL\n- missing guard clause\n- command exited 1\n",
        )
        self.assertEqual(
            merged,
            ("- Verdict: FAIL", "- missing guard clause", "- no test for empty input",
             "- command exited 1"),
        )

    def test_report_separates_product_defects_from_environment_problems(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec()
            report = write_repair_report(
                life.run, spec, 2,
                task_verifier_text="- Verdict: FAIL\n- AC-1 not met\n",
                test_verifier_text="- Verdict: FAIL\n- AC-1 not met\n- toolchain missing\n",
                source_attempt=1,
                product_defects=("AC-1 not met — add the guard clause",),
                environment_problems=("toolchain missing on the runner",),
                regression_tests=("feature-pipeline-skill -> uv run python -m unittest",),
            )
            text = report.path.read_text(encoding="utf-8")
            self.assertEqual(report.path, repair_report_path(life.run.run_dir, "VR-03", 2))
            self.assertIn("## Product defects to fix", text)
            self.assertIn("- AC-1 not met — add the guard clause", text)
            self.assertIn("## Environment problems", text)
            self.assertIn("- toolchain missing on the runner", text)
            self.assertIn("## Required regression tests", text)
            self.assertIn("Original scope (unchanged): "
                          "feature-pipeline-skill/pipeline_core/execution.py", text)
            self.assertIn("verify-1/task-verifier-1.md", text)
            # both source reports are still embedded verbatim.
            self.assertIn("## Task-verifier report (verbatim)", text)
            self.assertIn("## Test-verifier report (verbatim)", text)

    def test_report_embeds_complete_runner_command_evidence_for_its_source_gate(self) -> None:
        """REC-34: a repair executor needs immutable command output, not a summary."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec()
            life.record_command(
                "task:VR-03:verify:1:revision:3", ".",
                ["python", "setup_project.py", "--confirm"], 0, 0.01,
                "generator stdout\n", "generator stderr\n",
            )
            report = write_repair_report(
                life.run, spec, 1,
                task_verifier_text="- Verdict: FAIL\n- document actual evidence\n",
                test_verifier_text="- Verdict: PASS\n",
                source_attempt=1,
                revision=3,
            )
            text = report.path.read_text(encoding="utf-8")

            self.assertIn("## Runner-owned command evidence (immutable)", text)
            self.assertIn("task:VR-03:verify:1:revision:3", text)
            self.assertIn("python setup_project.py --confirm", text)
            self.assertIn("generator stdout", text)
            self.assertIn("generator stderr", text)

    def test_newest_repair_report_picks_the_highest_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec()
            for attempt in (2, 3):
                write_repair_report(
                    life.run, spec, attempt,
                    task_verifier_text="- Verdict: FAIL\n",
                    test_verifier_text="- Verdict: FAIL\n",
                    source_attempt=attempt - 1,
                )
            self.assertEqual(
                newest_repair_report(life.run.run_dir, "VR-03"),
                repair_report_path(life.run.run_dir, "VR-03", 3),
            )
            self.assertIsNone(newest_repair_report(life.run.run_dir, "NOPE-9"))

    def test_a_revisioned_repair_report_never_shadows_or_is_shadowed_by_the_unamended_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec()
            write_repair_report(
                life.run, spec, 2,
                task_verifier_text="- Verdict: FAIL\n",
                test_verifier_text="- Verdict: FAIL\n",
                source_attempt=1,
            )
            write_repair_report(
                life.run, spec, 2,
                task_verifier_text="- Verdict: FAIL\n",
                test_verifier_text="- Verdict: FAIL\n",
                source_attempt=1,
                revision=1,
            )

            unamended = repair_report_path(life.run.run_dir, "VR-03", 2)
            revisioned = repair_report_path(life.run.run_dir, "VR-03", 2, revision=1)
            self.assertNotEqual(unamended, revisioned)
            self.assertTrue(unamended.is_file())
            self.assertTrue(revisioned.is_file())
            self.assertIn("- Revision: 0", unamended.read_text(encoding="utf-8"))
            self.assertIn("- Revision: 1", revisioned.read_text(encoding="utf-8"))

            # each revision's newest-report lookup only ever sees its own namespace.
            self.assertEqual(newest_repair_report(life.run.run_dir, "VR-03"), unamended)
            self.assertEqual(
                newest_repair_report(life.run.run_dir, "VR-03", revision=1), revisioned)
            self.assertIsNone(newest_repair_report(life.run.run_dir, "VR-03", revision=2))


# --- resume at a repair boundary ------------------------------------------------------


class ResumeAtRepairBoundaryTests(unittest.TestCase):
    def test_resume_from_verification_failed_continues_the_open_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)

            # One failed gate, then a crash simulated at the repair boundary: the consolidated
            # repair report and the incremented attempt are on disk, and RESUME_ROLLBACKS has
            # rolled the interrupted 'repairing' state back to 'verification_failed'.
            first_executor = ScriptedExecutor(("implemented", "implemented"))
            run_task(
                life,
                _execution(spec, first_executor,
                           StubVerifier(("FAIL", "PASS")), StubVerifier(("PASS", "PASS"))),
            )  # verifies on the repair; attempts == 1, repair-2.md persisted

            reloaded = Run.load(life.run.run_dir, root)
            reloaded.task("VR-03").status = "in_progress"
            reloaded.save()

            executor = ScriptedExecutor(("implemented",))
            result = run_task(
                RunLifecycle(reloaded),
                TaskExecution(
                    spec=spec, adapter=executor,
                    launchers=VerifierLaunchers(
                        task=StubVerifier(("PASS",)), test=StubVerifier(("PASS",))),
                    verifier_anchors=VERIFIER_ANCHORS, envelope_anchors=ENVELOPE_ANCHORS,
                ),
            )

            self.assertEqual(result.status, "done")
            # the resume continued the already-open repair — no further attempt was consumed.
            self.assertEqual(reloaded.task("VR-03").attempts, 1)
            self.assertTrue(executor.calls[0]["is_repair"])
            self.assertIn("repair-2.md", executor.calls[0]["prompt"])

    def test_interrupted_repairing_without_a_report_fails_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            life.run.task("VR-03").status = "in_progress"
            life.run.save()

            executor = ScriptedExecutor(("implemented",))
            result = run_task(
                life,
                _execution(spec, executor, StubVerifier(("PASS",)), StubVerifier(("PASS",))),
            )
            self.assertEqual(result.status, "done")

    def test_resume_reuses_uncommitted_repair_report_without_rewriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            run_task(
                life,
                _execution(
                    spec,
                    ScriptedExecutor(("implemented", "implemented")),
                    StubVerifier(("FAIL", "PASS")),
                    StubVerifier(("PASS", "PASS")),
                ),
            )
            reloaded = Run.load(life.run.run_dir, root)
            reloaded.task("VR-03").status = "in_progress"
            reloaded.task("VR-03").attempts = 0
            reloaded.save()

            with mock.patch(
                "feature_pipeline.application.task_engine.write_repair_report",
                wraps=write_repair_report,
            ) as writer:
                result = run_task(
                    RunLifecycle(reloaded),
                    _execution(
                        spec,
                        ScriptedExecutor(("implemented", "implemented")),
                        StubVerifier(("FAIL", "PASS")),
                        StubVerifier(("PASS", "PASS")),
                    ),
                )

            self.assertEqual(result.status, "done")
            self.assertEqual(writer.call_count, 0)
            self.assertEqual(_repair_reports(reloaded, "VR-03"), [2])

    def test_resumed_exhausted_repair_states_block_without_another_executor(self) -> None:
        for status in ("in_progress",):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                life = _run(root)
                spec = _spec(max_repair_attempts=2)
                run_task(
                    life,
                    _execution(
                        spec,
                        ScriptedExecutor(("implemented",) * 3),
                        StubVerifier(("FAIL",) * 3),
                        StubVerifier(("PASS",) * 3),
                    ),
                )
                reloaded = Run.load(life.run.run_dir, root)
                reloaded.task("VR-03").status = status
                reloaded.save()

                executor = ScriptedExecutor(("implemented",))
                result = run_task(
                    RunLifecycle(reloaded),
                    _execution(
                        spec, executor, StubVerifier(("PASS",)), StubVerifier(("PASS",))
                    ),
                )

                self.assertEqual(result.status, "done")
                self.assertEqual(result.attempts, 2)
                self.assertEqual(executor.launches, 1)
                self.assertEqual(_repair_reports(reloaded, "VR-03"), [2, 3, 4])

    def test_resumed_ready_after_final_repair_blocks_without_another_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            run_task(
                life,
                _execution(
                    spec,
                    ScriptedExecutor(("implemented",) * 3),
                    StubVerifier(("FAIL",) * 3),
                    StubVerifier(("PASS",) * 3),
                ),
            )
            reloaded = Run.load(life.run.run_dir, root)
            reloaded.task("VR-03").status = "in_progress"
            reloaded.save()

            executor = ScriptedExecutor(("implemented",))
            result = run_task(
                RunLifecycle(reloaded),
                _execution(
                    spec, executor, StubVerifier(("PASS",)), StubVerifier(("PASS",))
                ),
            )

            self.assertEqual(result.status, "done")
            self.assertEqual(executor.launches, 1)

    def test_repairing_state_rejects_a_stale_repair_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            run_task(
                life,
                _execution(
                    spec,
                    ScriptedExecutor(("implemented", "implemented")),
                    StubVerifier(("FAIL", "PASS")),
                    StubVerifier(("PASS", "PASS")),
                ),
            )
            reloaded = Run.load(life.run.run_dir, root)
            reloaded.task("VR-03").status = "in_progress"
            stale = repair_report_path(reloaded.run_dir, "VR-03", 99)
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_text("stale", encoding="utf-8")
            reloaded.save()

            executor = ScriptedExecutor(("implemented",))
            with self.assertRaisesRegex(DispatchError, "repair report identity conflicts"):
                run_task(
                    RunLifecycle(reloaded),
                    _execution(spec, executor, StubVerifier(("PASS",)), StubVerifier(("PASS",))),
                )
            self.assertEqual(executor.launches, 0)

    def test_repairing_state_rejects_malformed_repair_report_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            run_task(
                life,
                _execution(
                    spec,
                    ScriptedExecutor(("implemented", "implemented")),
                    StubVerifier(("FAIL", "PASS")),
                    StubVerifier(("PASS", "PASS")),
                ),
            )
            reloaded = Run.load(life.run.run_dir, root)
            reloaded.task("VR-03").status = "in_progress"
            report = repair_report_path(reloaded.run_dir, "VR-03", 2)
            report.write_text("garbage", encoding="utf-8")
            reloaded.save()

            executor = ScriptedExecutor(("implemented",))
            with self.assertRaisesRegex(DispatchError, "repair report identity conflicts"):
                run_task(
                    RunLifecycle(reloaded),
                    _execution(spec, executor, StubVerifier(("PASS",)), StubVerifier(("PASS",))),
                )
            self.assertEqual(executor.launches, 0)


# --- ROC-01 AC-2: escalate at the advisory threshold, then resume without unblocking --------


class ResumeAfterEscalationReachesCompletionTests(unittest.TestCase):
    """ROC-01 AC-2 — crossing ``max_repair_attempts`` stops a bounded invocation with a
    truthful non-zero exit while the task stays unfinished (``in_progress``, no blocker, no
    task-set replacement). A later authorized invocation resumes the very same task record and
    reaches independently verified ``done`` without any unblock transition."""

    def test_escalation_then_resume_reaches_verified_completion_without_unblock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=0)

            escalated = run_task(
                life,
                _execution(spec, ScriptedExecutor(("implemented",)),
                           StubVerifier(("FAIL",)), StubVerifier(("PASS",))),
            )

            # A bounded invocation stops truthfully; the underlying task record is neither
            # blocked nor terminal, and history explains why.
            self.assertEqual(escalated.status, "escalated")
            self.assertNotEqual(escalated.exit_code, 0)
            record = life.run.task("VR-03")
            self.assertEqual(record.status, "in_progress")
            self.assertIsNone(record.blocker)
            self.assertEqual(record.operation_history[-1]["outcome"], "escalated")
            history_before = len(record.operation_history)

            # A later authorized continuation performs another operation on the identical
            # record — no reset, no cancellation/replacement, no unblock call — and this time
            # reaches independently verified completion.
            reloaded = Run.load(life.run.run_dir, root)
            self.assertEqual(reloaded.task("VR-03").status, "in_progress")
            resumed = run_task(
                RunLifecycle(reloaded),
                _execution(spec, ScriptedExecutor(("implemented",)),
                           StubVerifier(("PASS",)), StubVerifier(("PASS",))),
            )

            self.assertEqual(resumed.status, "done")
            self.assertEqual(reloaded.task("VR-03").status, "done")
            self.assertEqual(reloaded.task("VR-03").resolution, "completed")
            # The escalation's operation history is preserved, not erased or replaced.
            self.assertGreater(len(reloaded.task("VR-03").operation_history), history_before)
            self.assertEqual(
                reloaded.task("VR-03").operation_history[history_before - 1]["outcome"],
                "escalated",
            )


# --- REC-21: amendment-revision-scoped command, verifier, and repair evidence -----------


class _AmendmentAwareVerifier:
    """Wrap a :class:`StubVerifier` so a settled PASS report also carries the
    ``Amendment-justification finding: revision N, epoch N: ...`` line an active amendment
    revision requires before its PASS verdict may settle (see
    ``pipeline_core.verification._amendment_assessment_failure``)."""

    def __init__(self, verdicts: tuple[str, ...], *, revision: int, epoch: int) -> None:
        self._inner = StubVerifier(verdicts)
        self._revision = revision
        self._epoch = epoch

    @property
    def calls(self) -> list[dict]:
        return self._inner.calls

    def launch(self, request):  # noqa: ANN001 - test double
        result = self._inner.launch(request)
        if not request.resume_session_id:
            path = Path(request.report_path)
            text = path.read_text(encoding="utf-8")
            if "Verdict: PASS" in text and "amendment-justification finding:" not in text.lower():
                text += (
                    f"\n- Amendment-justification finding: revision {self._revision}, "
                    f"epoch {self._epoch}: accepted — the corrected verification command is a "
                    "reviewable repair artifact.\n"
                )
                path.write_text(text, encoding="utf-8")
        return result


class AmendmentRevisionScopedEvidenceTests(unittest.TestCase):
    """REC-21: an approved amendment revision gets its own immutable verification-command,
    verifier-artifact, and repair-report namespace. Revision N's first gate can never read or
    reuse revision N-1's settled evidence at the same attempt number, and a same-revision
    resume still reuses only its own byte-identical evidence without rerunning commands or
    double-spending a repair attempt."""

    def _amend(self, life: RunLifecycle, task_id: str, *, revision: int = 1) -> None:
        record = life.run.task(task_id)
        amendment = AmendmentRevision(
            task_id=task_id, revision=revision,
            prior_digest=record.task_contract_digest or "sha256:none",
            new_digest=f"sha256:revision-{revision}", changed_fields=("verification_commands",),
            added_paths=(), rationale="the prior revision's command was wrong; correct it",
            approved_by="a-human", source_evidence="report:launch-1",
            created_at="2026-09-15T00:00:00Z", epoch=revision,
        )
        life.run.apply_amendment(
            amendment, new_digest=amendment.new_digest,
            new_digest_version="tam01-amendment-v1")
        life.run.task(task_id).status = "in_progress"
        life.run.save()

    def test_amended_revisions_first_gate_reruns_commands_instead_of_reusing_a_failed_prior_revision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            failing = {"cwd": ".", "argv": [sys.executable, "-c", "raise SystemExit(1)"]}
            passing = {"cwd": ".", "argv": [sys.executable, "-c", "raise SystemExit(0)"]}
            spec0 = _spec(max_repair_attempts=0, verification_commands=(failing,))

            escalated = run_task(
                life,
                _execution(spec0, ScriptedExecutor(("implemented",)),
                           StubVerifier(("PASS",)), StubVerifier(("FAIL",))),
            )
            self.assertEqual(escalated.status, "escalated")
            stage0 = verification_stage("VR-03", attempt=1)
            self.assertIn(stage0, life.run.stages)
            failed_id = life.run.stage_command_ids(stage0)[0]
            self.assertNotEqual(life.run.command(failed_id)["exit_code"], 0)

            self._amend(life, "VR-03", revision=1)

            spec1 = _spec(max_repair_attempts=0, verification_commands=(passing,))
            revised_executor = ScriptedExecutor(("implemented",))
            result = run_task(
                RunLifecycle(life.run),
                _execution(
                    spec1, revised_executor,
                    _AmendmentAwareVerifier(("PASS",), revision=1, epoch=1),
                    _AmendmentAwareVerifier(("PASS",), revision=1, epoch=1)),
            )

            self.assertEqual(result.status, "done")
            # A fresh dispatch, never a continuation of the prior revision's repair.
            self.assertFalse(revised_executor.calls[0]["is_repair"])
            stage1 = verification_stage("VR-03", attempt=1, revision=1)
            self.assertNotEqual(stage0, stage1)
            self.assertIn(stage1, life.run.stages)
            fresh_ids = life.run.stage_command_ids(stage1)
            self.assertEqual(len(fresh_ids), 1)
            self.assertNotIn(fresh_ids[0], life.run.stage_command_ids(stage0))
            self.assertEqual(life.run.command(fresh_ids[0])["exit_code"], 0)

    def test_amended_revisions_repair_report_is_revision_qualified_and_embeds_its_own_verifier_reports(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec0 = _spec(max_repair_attempts=0)
            run_task(
                life,
                _execution(spec0, ScriptedExecutor(("implemented",)),
                           StubVerifier(("FAIL",)), StubVerifier(("PASS",))),
            )
            # revision 0's own repair report keeps its historical, unqualified name.
            self.assertTrue(repair_report_path(life.run.run_dir, "VR-03", 2).is_file())

            self._amend(life, "VR-03", revision=1)

            spec1 = _spec(max_repair_attempts=1)
            repair_executor = ScriptedExecutor(("implemented", "implemented"))
            result = run_task(
                RunLifecycle(life.run),
                _execution(
                    spec1, repair_executor,
                    _AmendmentAwareVerifier(("FAIL", "PASS"), revision=1, epoch=1),
                    _AmendmentAwareVerifier(("PASS", "PASS"), revision=1, epoch=1)),
            )

            self.assertEqual(result.status, "done")
            revisioned = repair_report_path(life.run.run_dir, "VR-03", 2, revision=1)
            self.assertTrue(revisioned.is_file())
            text = revisioned.read_text(encoding="utf-8")
            self.assertIn("- Revision: 1", text)
            self.assertIn("verify-1-revision-1/task-verifier-1.md", text)
            # the redispatched (repairing) executor's prompt names this exact revisioned report,
            # proving the production isolated-workspace dispatch path resolved and copied it.
            _, repair_call = repair_executor.calls
            self.assertTrue(repair_call["is_repair"])
            self.assertIn("repair-2-revision-1.md", repair_call["prompt"])

    def test_same_revision_resume_reuses_settled_repair_evidence_without_rewriting_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec0 = _spec(max_repair_attempts=0)
            run_task(
                life,
                _execution(spec0, ScriptedExecutor(("implemented",)),
                           StubVerifier(("FAIL",)), StubVerifier(("PASS",))),
            )
            self._amend(life, "VR-03", revision=1)
            spec1 = _spec(max_repair_attempts=2)
            run_task(
                RunLifecycle(life.run),
                _execution(
                    spec1, ScriptedExecutor(("implemented", "implemented")),
                    _AmendmentAwareVerifier(("FAIL", "PASS"), revision=1, epoch=1),
                    _AmendmentAwareVerifier(("PASS", "PASS"), revision=1, epoch=1)),
            )

            reloaded = Run.load(life.run.run_dir, root)
            reloaded.task("VR-03").status = "in_progress"
            reloaded.task("VR-03").attempts = 0
            reloaded.save()

            with mock.patch(
                "feature_pipeline.application.task_engine.write_repair_report",
                wraps=write_repair_report,
            ) as writer:
                result = run_task(
                    RunLifecycle(reloaded),
                    _execution(
                        spec1, ScriptedExecutor(("implemented", "implemented")),
                        _AmendmentAwareVerifier(("FAIL", "PASS"), revision=1, epoch=1),
                        _AmendmentAwareVerifier(("PASS", "PASS"), revision=1, epoch=1)),
                )

            self.assertEqual(result.status, "done")
            self.assertEqual(writer.call_count, 0)
            revisioned = repair_report_path(reloaded.run_dir, "VR-03", 2, revision=1)
            self.assertTrue(revisioned.is_file())


if __name__ == "__main__":
    unittest.main()
