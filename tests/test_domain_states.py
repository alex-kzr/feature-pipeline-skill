"""Three-state product-progress transition policy."""

from __future__ import annotations

import unittest

from feature_pipeline.domain import states
from feature_pipeline.domain.errors import DomainError
from feature_pipeline.domain.states import (
    IllegalTransition,
    RunState,
    TaskState,
    Transition,
    UnauthorizedTransition,
    apply,
)
from feature_pipeline.domain.vocabulary import Actor, DoneResolution, TaskStatus


class ThreeStateTransitionsTests(unittest.TestCase):
    def _state(self, status: TaskStatus = TaskStatus.TO_DO) -> RunState:
        return RunState.plan("demo", [TaskState("T-1", status=status)])

    def test_only_three_product_statuses_exist(self) -> None:
        self.assertEqual(TaskStatus.values(), ("to_do", "in_progress", "done"))

    def test_progress_moves_from_to_do_to_in_progress_to_completed_done(self) -> None:
        state = apply(self._state(), Transition("T-1", TaskStatus.IN_PROGRESS))
        state = apply(state, Transition("T-1", TaskStatus.DONE,
                                        resolution=DoneResolution.COMPLETED))
        self.assertEqual(state.task("T-1").resolution, DoneResolution.COMPLETED)

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


if __name__ == "__main__":
    unittest.main()
