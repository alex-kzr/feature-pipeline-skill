"""Three-state product-progress transition policy."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace as dc_replace
from pathlib import Path

from feature_pipeline.domain import states
from feature_pipeline.domain.errors import DomainError
from feature_pipeline.domain.repair import RepairBound
from feature_pipeline.domain.states import (
    BeginRepair,
    IllegalTransition,
    RecordVerdicts,
    Repair,
    RepairBudgetExhausted,
    RunState,
    TaskState,
    Transition,
    UnauthorizedTransition,
    apply,
)
from feature_pipeline.domain.vocabulary import Actor, DoneResolution, TaskStatus, Verdict


class ThreeStateTransitionsTests(unittest.TestCase):
    def _state(self, status: TaskStatus = TaskStatus.TO_DO) -> RunState:
        return RunState.plan("demo", [TaskState("T-1", status=status)])

    def test_only_three_product_statuses_exist(self) -> None:
        self.assertEqual(TaskStatus.values(), ("to_do", "in_progress", "done"))

    def test_progress_moves_from_to_do_to_in_progress_to_completed_done(self) -> None:
        state = apply(self._state(), Transition("T-1", TaskStatus.IN_PROGRESS))
        state = apply(state, RecordVerdicts("T-1", Verdict.PASS, Verdict.PASS))
        state = apply(state, Transition("T-1", TaskStatus.DONE,
                                        resolution=DoneResolution.COMPLETED))
        self.assertEqual(state.task("T-1").resolution, DoneResolution.COMPLETED)

    def test_completed_resolution_rejects_missing_verification_evidence(self) -> None:
        state = apply(self._state(), Transition("T-1", TaskStatus.IN_PROGRESS))
        with self.assertRaises(IllegalTransition):
            apply(state, Transition("T-1", TaskStatus.DONE,
                                    resolution=DoneResolution.COMPLETED))

    def test_completed_resolution_rejects_a_failed_verdict(self) -> None:
        state = apply(self._state(), Transition("T-1", TaskStatus.IN_PROGRESS))
        state = apply(state, RecordVerdicts("T-1", Verdict.FAIL, Verdict.PASS))
        with self.assertRaises(IllegalTransition):
            apply(state, Transition("T-1", TaskStatus.DONE,
                                    resolution=DoneResolution.COMPLETED))
        # The failure is durable operation evidence; the task stays unfinished, never blocked.
        self.assertEqual(state.task("T-1").status, TaskStatus.IN_PROGRESS)

    def test_completed_resolution_rejects_a_task_that_never_started(self) -> None:
        with self.assertRaises(IllegalTransition):
            apply(self._state(), Transition("T-1", TaskStatus.DONE,
                                            resolution=DoneResolution.COMPLETED))

    def test_human_may_cancel_a_task_that_has_not_started(self) -> None:
        state = apply(self._state(), Transition("T-1", TaskStatus.DONE, actor=Actor.HUMAN,
                                                resolution=DoneResolution.CANCELLED,
                                                note="no longer needed"))
        task = state.task("T-1")
        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertEqual(task.resolution, DoneResolution.CANCELLED)
        self.assertEqual(task.resolution_reason, "no longer needed")

    def test_cancelling_a_task_that_has_not_started_requires_a_reason(self) -> None:
        with self.assertRaises(IllegalTransition):
            apply(self._state(), Transition("T-1", TaskStatus.DONE, actor=Actor.HUMAN,
                                            resolution=DoneResolution.CANCELLED, note=""))

    def test_human_may_cancel_a_task_in_progress(self) -> None:
        state = apply(self._state(), Transition("T-1", TaskStatus.IN_PROGRESS))
        state = apply(state, Transition("T-1", TaskStatus.DONE, actor=Actor.HUMAN,
                                        resolution=DoneResolution.CANCELLED,
                                        note="requirements changed"))
        task = state.task("T-1")
        self.assertEqual(task.resolution, DoneResolution.CANCELLED)
        self.assertEqual(task.resolution_reason, "requirements changed")

    def test_executor_cannot_cancel_a_task(self) -> None:
        with self.assertRaises(UnauthorizedTransition):
            apply(self._state(), Transition("T-1", TaskStatus.DONE, actor=Actor.EXECUTOR,
                                            resolution=DoneResolution.CANCELLED,
                                            note="not my call"))

    def test_executor_cannot_complete_a_task(self) -> None:
        state = apply(self._state(), Transition("T-1", TaskStatus.IN_PROGRESS))
        state = apply(state, RecordVerdicts("T-1", Verdict.PASS, Verdict.PASS))
        with self.assertRaises(UnauthorizedTransition):
            apply(state, Transition("T-1", TaskStatus.DONE, actor=Actor.EXECUTOR,
                                    resolution=DoneResolution.COMPLETED))

    def test_a_failed_verdict_leaves_the_task_unfinished_not_blocked(self) -> None:
        state = apply(self._state(), Transition("T-1", TaskStatus.IN_PROGRESS))
        state = apply(state, RecordVerdicts("T-1", Verdict.FAIL, Verdict.FAIL))
        task = state.task("T-1")
        self.assertEqual(task.status, TaskStatus.IN_PROGRESS)
        self.assertIsNone(task.resolution)

    def test_repair_budget_exhaustion_leaves_the_task_unfinished_not_blocked(self) -> None:
        state = apply(self._state(), Transition("T-1", TaskStatus.IN_PROGRESS))
        task = state.task("T-1")
        exhausted = dc_replace(task, repair=Repair(attempts=2, bound=RepairBound(2)))
        state = state.with_task(exhausted)
        with self.assertRaises(RepairBudgetExhausted):
            apply(state, BeginRepair("T-1"))
        # The advisory threshold is escalation evidence; the task remains in progress, not
        # blocked and not terminal.
        self.assertEqual(state.task("T-1").status, TaskStatus.IN_PROGRESS)
        self.assertFalse(state.task("T-1").is_terminal)

    def test_cancellation_requires_a_human_readable_reason(self) -> None:
        state = apply(self._state(), Transition("T-1", TaskStatus.IN_PROGRESS))
        with self.assertRaises(IllegalTransition):
            apply(state, Transition("T-1", TaskStatus.DONE,
                                    resolution=DoneResolution.CANCELLED, note="  "))

    def test_done_cannot_be_constructed_without_a_resolution(self) -> None:
        with self.assertRaises(DomainError):
            TaskState("T-1", status=TaskStatus.DONE)

    def test_done_can_only_be_reopened_by_a_human_with_a_reason(self) -> None:
        state = apply(self._state(), Transition("T-1", TaskStatus.IN_PROGRESS))
        state = apply(state, RecordVerdicts("T-1", Verdict.PASS, Verdict.PASS))
        state = apply(state, Transition("T-1", TaskStatus.DONE,
                                        resolution=DoneResolution.COMPLETED))
        with self.assertRaises(UnauthorizedTransition):
            apply(state, Transition("T-1", TaskStatus.IN_PROGRESS, actor=Actor.RUNNER,
                                    note="retry"))
        with self.assertRaises(IllegalTransition):
            apply(state, Transition("T-1", TaskStatus.IN_PROGRESS, actor=Actor.HUMAN))
        reopened = apply(state, Transition("T-1", TaskStatus.IN_PROGRESS, actor=Actor.HUMAN,
                                           note="customer requested follow-up"))
        self.assertEqual(reopened.task("T-1").status, TaskStatus.IN_PROGRESS)

    def test_blocked_is_not_a_transition_target(self) -> None:
        targets = {target.value for values in states.TASK_TRANSITIONS.values() for target in values}
        self.assertNotIn("blocked", targets)

    def test_transition_policy_matches_the_portable_compatibility_layer(self) -> None:
        """The typed domain and ``pipeline_core.state`` agree on reachable targets (AC-1)."""
        from pipeline_core import state as legacy_state

        typed = {
            status.value: {target.value for target in targets}
            for status, targets in states.TASK_TRANSITIONS.items()
        }
        self.assertEqual(typed, legacy_state.TASK_TRANSITIONS)


class PortableCompatibilityLayerCancellationTests(unittest.TestCase):
    """``pipeline_core.state.Run`` mirrors the typed domain's cancellation/completion authority."""

    def _run(self, root: Path):
        from pipeline_core.state import Run

        prompt = root / "prompt.md"
        prompt.write_text("feature", encoding="utf-8")
        run = Run.create("demo", prompt, None, root / "runs" / "demo", root)
        run.add_task("T-1")
        return run

    def test_human_may_cancel_a_task_that_has_not_started(self) -> None:
        from pipeline_core.state import ACTOR_HUMAN

        with tempfile.TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            run.transition_task("T-1", "done", ACTOR_HUMAN, note="no longer needed",
                               resolution="cancelled")
            record = run.task("T-1")
            self.assertEqual(record.status, "done")
            self.assertEqual(record.resolution, "cancelled")
            self.assertEqual(record.resolution_reason, "no longer needed")

    def test_cancelling_a_task_that_has_not_started_requires_a_reason(self) -> None:
        from pipeline_core.state import ACTOR_HUMAN, TransitionError

        with tempfile.TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            with self.assertRaises(TransitionError) as caught:
                run.transition_task("T-1", "done", ACTOR_HUMAN, resolution="cancelled")
            self.assertEqual(caught.exception.code, "missing-cancellation-reason")

    def test_executor_cannot_cancel_a_task(self) -> None:
        from pipeline_core.state import ACTOR_EXECUTOR, TransitionError

        with tempfile.TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            with self.assertRaises(TransitionError) as caught:
                run.transition_task("T-1", "done", ACTOR_EXECUTOR, note="not my call",
                                   resolution="cancelled")
            self.assertEqual(caught.exception.code, "unauthorized-transition")

    def test_completed_resolution_rejects_a_task_that_never_started(self) -> None:
        from pipeline_core.state import ACTOR_RUNNER, TransitionError

        with tempfile.TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            with self.assertRaises(TransitionError) as caught:
                run.transition_task("T-1", "done", ACTOR_RUNNER, resolution="completed")
            self.assertEqual(caught.exception.code, "illegal-transition")

    def test_completed_resolution_requires_two_passing_verdicts(self) -> None:
        from pipeline_core.state import ACTOR_RUNNER, TransitionError

        with tempfile.TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            run.transition_task("T-1", "in_progress", ACTOR_RUNNER)
            with self.assertRaises(TransitionError) as caught:
                run.transition_task("T-1", "done", ACTOR_RUNNER, resolution="completed")
            self.assertEqual(caught.exception.code, "missing-verification-evidence")
            self.assertEqual(run.task("T-1").status, "in_progress")

    def test_completed_resolution_rejects_failed_verdict_evidence(self) -> None:
        from pipeline_core.state import ACTOR_RUNNER, TransitionError

        with tempfile.TemporaryDirectory() as directory:
            run = self._run(Path(directory))
            run.transition_task("T-1", "in_progress", ACTOR_RUNNER)
            run.record_verdicts("T-1", "FAIL", "PASS")
            with self.assertRaises(TransitionError) as caught:
                run.transition_task("T-1", "done", ACTOR_RUNNER, resolution="completed")
            self.assertEqual(caught.exception.code, "missing-verification-evidence")
            self.assertEqual(run.task("T-1").status, "in_progress")


class AmendmentRevisionStaysWithinThreeProductStatesTests(unittest.TestCase):
    """TAM-01: an amendment revises a task's *contract*, never its product-progress status —
    the three-state policy (``to_do``/``in_progress``/``done``) is unaffected (AC-2, AC-3)."""

    def test_amendment_lifecycle_adds_no_fourth_product_status(self) -> None:
        self.assertEqual(TaskStatus.values(), ("to_do", "in_progress", "done"))

    def test_apply_amendment_leaves_an_in_progress_task_in_progress(self) -> None:
        from pipeline_core.plan import AmendmentRevision
        from pipeline_core.state import Run

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("feature", encoding="utf-8")
            run = Run.create("demo", prompt, None, root / "runs" / "demo", root)
            run.add_task("T-1")
            run.transition_task("T-1", "in_progress")
            revision = AmendmentRevision(
                task_id="T-1", revision=1, prior_digest="sha256:a", new_digest="sha256:b",
                changed_fields=("allowed_scope",), added_paths=("tests/new_fixture.py",),
                rationale="baseline exposed an out-of-scope failure",
                approved_by="a-human", source_evidence="report:launch-1",
                created_at="2026-09-12T00:00:00Z", epoch=1,
            )
            run.apply_amendment(revision, new_digest="sha256:b",
                                new_digest_version="tam01-amendment-v1")
            self.assertEqual(run.task("T-1").status, "in_progress")


if __name__ == "__main__":
    unittest.main()
