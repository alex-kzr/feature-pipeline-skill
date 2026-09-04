"""DS-01 — typed durable state and pure lifecycle transitions.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 5),
``docs/plans/tasks/DS-01_typed-state-transitions.md``.

Three things are pinned here:

* **AC-1** — the domain state module holds no persistence path and writes no file: it imports
  nothing from :mod:`os` / :mod:`json` / :mod:`pathlib`, its value objects are all frozen, and
  no field is a path.
* **AC-2** — every transition outside :data:`states.TASK_TRANSITIONS`, every unauthorized
  actor, and every exhausted repair budget is rejected deterministically with a typed
  :class:`~feature_pipeline.domain.errors.DomainError` subclass and its stable ``code``; the
  input state is never mutated.
* **AC-3** — a scripted lifecycle produces the same ``status`` / ``attempts`` / verification
  record when driven through the pure :func:`states.apply` as when driven through the live
  ``pipeline_core.state.Run``; the transition table, resume rollbacks, and the empty
  verification/evidence shapes match the live constants.

Standard library only.
"""

from __future__ import annotations

import dataclasses
import inspect
import tempfile
import unittest
from pathlib import Path

from feature_pipeline.domain import states
from feature_pipeline.domain.errors import DomainError
from feature_pipeline.domain.repair import RepairBound
from feature_pipeline.domain.states import (
    BeginRepair,
    Evidence,
    IllegalTransition,
    LaunchGenerations,
    RecordVerdicts,
    RepairBudgetExhausted,
    Resume,
    RunState,
    TaskState,
    Transition,
    UnauthorizedTransition,
    UnknownTask,
    UnknownVerdict,
    Verification,
    apply,
)
from feature_pipeline.domain.vocabulary import Actor, RunStatus, TaskStatus, Verdict

from pipeline_core import state as legacy


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------
_ALL_STATUSES = tuple(TaskStatus)


def _one(status: TaskStatus = TaskStatus.PENDING, *, bound: int = 2,
         attempts: int = 0) -> RunState:
    """A run with a single task ``T-1`` fixed at ``status``."""
    task = TaskState(
        id="T-1",
        status=status,
        repair=states.Repair(attempts=attempts, bound=RepairBound(bound)),
    )
    return RunState.plan("demo", [task])


def _legacy_run(root: Path, *, feature: str = "demo") -> "legacy.Run":
    prompt = root / "prompt.md"
    prompt.write_text("feature", encoding="utf-8")
    run = legacy.Run.create(feature, prompt, None, root / "run", root)
    run.add_task("T-1")
    return run


# ==========================================================================================
# AC-1 — no persistence, no file writes, frozen value objects
# ==========================================================================================
class PurityTests(unittest.TestCase):
    def test_module_imports_no_io_surface(self) -> None:
        source = inspect.getsource(states)
        for forbidden in ("import os", "import json", "from pathlib", "import pathlib",
                          "open(", ".write_text", ".read_text", "Path("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_module_namespace_exposes_no_filesystem_object(self) -> None:
        for name in ("os", "json", "Path", "open"):
            self.assertNotIn(name, vars(states))

    def test_every_value_object_is_frozen(self) -> None:
        for name in ("CommandRecord", "LaunchGenerations", "LaunchFailure", "Promotion",
                     "Attestation", "Verification", "Implementation", "Evidence", "Repair",
                     "TaskState", "RunState", "Transition", "RecordVerdicts", "BeginRepair",
                     "Resume"):
            cls = getattr(states, name)
            with self.subTest(cls=name):
                self.assertTrue(dataclasses.is_dataclass(cls))
                self.assertTrue(cls.__dataclass_params__.frozen)

    def test_no_field_is_a_path(self) -> None:
        for name in ("CommandRecord", "TaskState", "RunState", "Evidence", "Implementation"):
            cls = getattr(states, name)
            for f in dataclasses.fields(cls):
                with self.subTest(cls=name, field=f.name):
                    self.assertNotIn("path", f.name.lower())

    def test_apply_does_not_mutate_the_input_state(self) -> None:
        before = _one(TaskStatus.PENDING)
        snapshot = repr(before)
        after = apply(before, Transition("T-1", TaskStatus.READY))
        self.assertEqual(repr(before), snapshot)
        self.assertIsNot(after, before)
        self.assertEqual(before.task("T-1").status, TaskStatus.PENDING)
        self.assertEqual(after.task("T-1").status, TaskStatus.READY)


# ==========================================================================================
# AC-2 — forbidden transitions, terminal rules, actor authorization, bounded repair
# ==========================================================================================
class ForbiddenTransitionTableTests(unittest.TestCase):
    #: ``blocked -> implemented`` is in the table but the actor rules gate it to death in
    #: ``pipeline_core.state`` (only a human may touch a blocked task, and a human may only
    #: move it to ``ready``). The domain reproduces that exactly (AC-3).
    _ACTOR_GATED = {(TaskStatus.BLOCKED, TaskStatus.IMPLEMENTED)}

    def test_full_status_product_matches_the_table(self) -> None:
        for src in _ALL_STATUSES:
            allowed = states.TASK_TRANSITIONS[src]
            for dst in _ALL_STATUSES:
                run = _one(src)
                if dst not in allowed:
                    with self.subTest(src=src, dst=dst, expect="rejected"):
                        with self.assertRaises(IllegalTransition) as caught:
                            apply(run, Transition("T-1", dst, actor=Actor.RUNNER))
                        self.assertEqual(caught.exception.code, "illegal-transition")
                        self.assertEqual(run.task("T-1").status, src)  # input untouched
                elif (src, dst) in self._ACTOR_GATED:
                    with self.subTest(src=src, dst=dst, expect="actor-gated"):
                        for actor in Actor:
                            with self.assertRaises(UnauthorizedTransition):
                                apply(run, Transition("T-1", dst, actor=actor))
                else:
                    if dst is TaskStatus.IMPLEMENTED:
                        actor = Actor.EXECUTOR
                    elif src is TaskStatus.BLOCKED:
                        actor = Actor.HUMAN
                    else:
                        actor = Actor.RUNNER
                    with self.subTest(src=src, dst=dst, expect="allowed"):
                        self.assertEqual(
                            apply(run, Transition("T-1", dst, actor=actor)).task("T-1").status,
                            dst,
                        )

    def test_verified_is_the_only_terminal_state(self) -> None:
        self.assertEqual(states.TERMINAL_TASK_STATES, frozenset({TaskStatus.VERIFIED}))
        self.assertTrue(_one(TaskStatus.VERIFIED).task("T-1").is_terminal)
        for dst in _ALL_STATUSES:
            with self.subTest(dst=dst):
                with self.assertRaises(IllegalTransition):
                    apply(_one(TaskStatus.VERIFIED), Transition("T-1", dst))

    def test_unknown_task_fails_closed(self) -> None:
        with self.assertRaises(UnknownTask) as caught:
            apply(_one(), Transition("NOPE", TaskStatus.READY))
        self.assertEqual(caught.exception.code, "unknown-task")


class ActorAuthorizationTests(unittest.TestCase):
    def test_only_executor_or_runner_may_mark_implemented(self) -> None:
        run = _one(TaskStatus.RUNNING)
        with self.assertRaises(UnauthorizedTransition) as caught:
            apply(run, Transition("T-1", TaskStatus.IMPLEMENTED, actor=Actor.HUMAN))
        self.assertEqual(caught.exception.code, "unauthorized-transition")
        self.assertEqual(str(caught.exception),
                         "only an executor or runner may mark implemented")
        for actor in (Actor.EXECUTOR, Actor.RUNNER):
            with self.subTest(actor=actor):
                out = apply(_one(TaskStatus.RUNNING),
                            Transition("T-1", TaskStatus.IMPLEMENTED, actor=actor))
                self.assertEqual(out.task("T-1").status, TaskStatus.IMPLEMENTED)

    def test_only_the_runner_may_mark_verified(self) -> None:
        with self.assertRaises(UnauthorizedTransition) as caught:
            apply(_one(TaskStatus.IMPLEMENTED),
                  Transition("T-1", TaskStatus.VERIFIED, actor=Actor.EXECUTOR))
        self.assertEqual(str(caught.exception), "only the runner may mark verified")

    def test_only_a_human_may_unblock(self) -> None:
        with self.assertRaises(UnauthorizedTransition) as caught:
            apply(_one(TaskStatus.BLOCKED),
                  Transition("T-1", TaskStatus.READY, actor=Actor.RUNNER))
        self.assertEqual(str(caught.exception), "only a human may unblock a task")
        out = apply(_one(TaskStatus.BLOCKED),
                    Transition("T-1", TaskStatus.READY, actor=Actor.HUMAN))
        self.assertEqual(out.task("T-1").status, TaskStatus.READY)

    def test_blocked_note_is_recorded_as_the_blocker(self) -> None:
        out = apply(_one(TaskStatus.RUNNING),
                    Transition("T-1", TaskStatus.BLOCKED, note="held lease"))
        self.assertEqual(out.task("T-1").blocker, "held lease")


class VerdictSettlementTests(unittest.TestCase):
    def _implemented(self) -> RunState:
        return _one(TaskStatus.IMPLEMENTED)

    def test_both_pass_verifies(self) -> None:
        out = apply(self._implemented(),
                    RecordVerdicts("T-1", Verdict.PASS, "PASS", at="2026-09-04T00:00:00Z"))
        task = out.task("T-1")
        self.assertEqual(task.status, TaskStatus.VERIFIED)
        self.assertEqual(task.verification.as_record(),
                         {"task_verdict": "PASS", "test_verdict": "PASS",
                          "verified_at": "2026-09-04T00:00:00Z"})

    def test_any_fail_goes_to_verification_failed(self) -> None:
        for tv, xv in (("PASS", "FAIL"), ("FAIL", "PASS"), ("FAIL", "FAIL")):
            with self.subTest(tv=tv, xv=xv):
                out = apply(self._implemented(), RecordVerdicts("T-1", tv, xv))
                self.assertEqual(out.task("T-1").status, TaskStatus.VERIFICATION_FAILED)

    def test_blocked_takes_precedence_and_spends_no_attempt(self) -> None:
        for tv, xv in (("BLOCKED", "FAIL"), ("FAIL", "BLOCKED"), ("BLOCKED", "PASS")):
            with self.subTest(tv=tv, xv=xv):
                out = apply(self._implemented(), RecordVerdicts("T-1", tv, xv))
                task = out.task("T-1")
                self.assertEqual(task.status, TaskStatus.BLOCKED)
                self.assertEqual(task.blocker,
                                 "a verifier returned BLOCKED — external cause")
                self.assertEqual(task.attempts, 0)

    def test_unknown_verdict_token_fails_closed(self) -> None:
        with self.assertRaises(UnknownVerdict) as caught:
            apply(self._implemented(), RecordVerdicts("T-1", "PASS", "MAYBE"))
        self.assertEqual(caught.exception.code, "unknown-verdict")

    def test_verdicts_from_a_non_implemented_task_are_an_illegal_transition(self) -> None:
        with self.assertRaises(IllegalTransition):
            apply(_one(TaskStatus.PENDING), RecordVerdicts("T-1", "PASS", "PASS"))


class BoundedRepairTests(unittest.TestCase):
    def test_repair_attempts_are_bounded_then_the_task_can_be_blocked(self) -> None:
        run = _one(TaskStatus.VERIFICATION_FAILED, bound=2)
        run = apply(run, BeginRepair("T-1"))
        self.assertEqual(run.task("T-1").status, TaskStatus.REPAIRING)
        self.assertEqual(run.task("T-1").attempts, 1)

        # a fresh executor window, then FAIL again -> verification_failed
        run = apply(run, Transition("T-1", TaskStatus.RUNNING))
        run = apply(run, Transition("T-1", TaskStatus.IMPLEMENTED, actor=Actor.EXECUTOR))
        run = apply(run, RecordVerdicts("T-1", "FAIL", "FAIL"))
        run = apply(run, BeginRepair("T-1"))
        self.assertEqual(run.task("T-1").attempts, 2)

        run = apply(run, Transition("T-1", TaskStatus.RUNNING))
        run = apply(run, Transition("T-1", TaskStatus.IMPLEMENTED, actor=Actor.EXECUTOR))
        run = apply(run, RecordVerdicts("T-1", "FAIL", "FAIL"))
        with self.assertRaises(RepairBudgetExhausted) as caught:
            apply(run, BeginRepair("T-1"))
        self.assertEqual(caught.exception.code, "repair-budget-exhausted")
        self.assertEqual(run.task("T-1").status, TaskStatus.VERIFICATION_FAILED)
        # at the limit the caller blocks the task deterministically
        blocked = apply(run, Transition("T-1", TaskStatus.BLOCKED, note="repair budget spent"))
        self.assertEqual(blocked.task("T-1").status, TaskStatus.BLOCKED)

    def test_a_zero_budget_is_exhausted_immediately(self) -> None:
        with self.assertRaises(RepairBudgetExhausted):
            apply(_one(TaskStatus.VERIFICATION_FAILED, bound=0), BeginRepair("T-1"))

    def test_begin_repair_from_a_wrong_status_is_illegal(self) -> None:
        with self.assertRaises(IllegalTransition):
            apply(_one(TaskStatus.IMPLEMENTED), BeginRepair("T-1"))

    def test_full_repair_then_pass_reaches_verified(self) -> None:
        run = _one(TaskStatus.VERIFICATION_FAILED, bound=2)
        run = apply(run, BeginRepair("T-1"))
        run = apply(run, Transition("T-1", TaskStatus.RUNNING))
        run = apply(run, Transition("T-1", TaskStatus.IMPLEMENTED, actor=Actor.EXECUTOR))
        run = apply(run, RecordVerdicts("T-1", "PASS", "PASS"))
        self.assertEqual(run.task("T-1").status, TaskStatus.VERIFIED)
        self.assertEqual(run.task("T-1").attempts, 1)


class ResumeRollbackTests(unittest.TestCase):
    def test_rollback_table(self) -> None:
        expected = {
            TaskStatus.RUNNING: TaskStatus.READY,
            TaskStatus.REPAIRING: TaskStatus.VERIFICATION_FAILED,
        }
        for src in _ALL_STATUSES:
            with self.subTest(src=src):
                out = apply(_one(src), Resume())
                self.assertEqual(out.task("T-1").status, expected.get(src, src))

    def test_resume_is_idempotent(self) -> None:
        once = apply(_one(TaskStatus.RUNNING), Resume())
        twice = apply(once, Resume())
        self.assertEqual(once.task("T-1").status, twice.task("T-1").status)


class LaunchGenerationTests(unittest.TestCase):
    def test_consume_is_monotonic_and_per_role(self) -> None:
        gens = LaunchGenerations()
        first, gens = gens.consume("executor")
        second, gens = gens.consume("executor")
        tv_first, gens = gens.consume("task_verifier")
        self.assertEqual((first, second, tv_first), (1, 2, 1))
        self.assertEqual(gens.executor, 3)
        self.assertEqual(gens.test_verifier, 1)

    def test_unknown_role_fails_closed(self) -> None:
        with self.assertRaises(DomainError) as caught:
            LaunchGenerations().consume("reviewer")
        self.assertEqual(caught.exception.code, "unknown-launch-role")


# ==========================================================================================
# AC-3 — compatibility equivalence with the live ``pipeline_core.state`` policy
# ==========================================================================================
class LiveConstantParityTests(unittest.TestCase):
    def test_transition_table_matches_pipeline_core(self) -> None:
        mine = {k.value: {v.value for v in vs} for k, vs in states.TASK_TRANSITIONS.items()}
        theirs = {k: set(v) for k, v in legacy.TASK_TRANSITIONS.items()}
        self.assertEqual(mine, theirs)

    def test_resume_rollbacks_match_pipeline_core(self) -> None:
        mine = {k.value: v.value for k, v in states.RESUME_ROLLBACKS.items()}
        self.assertEqual(mine, dict(legacy.RESUME_ROLLBACKS))

    def test_empty_verification_shape_matches(self) -> None:
        self.assertEqual(Verification().as_record(), legacy._empty_verification())

    def test_empty_evidence_shape_matches(self) -> None:
        self.assertEqual(Evidence().as_record(), legacy._empty_execution_evidence())

    def test_verdict_tokens_match(self) -> None:
        self.assertEqual({v.value for v in Verdict}, set(legacy.VERDICT_TOKENS))


class LifecycleEquivalenceTests(unittest.TestCase):
    """Drive the same script through both engines; the task facts must agree."""

    def _legacy_facts(self, script: str) -> tuple[str, int, dict]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _legacy_run(root)
            run.transition_task("T-1", "ready")
            run.transition_task("T-1", "running")
            run.transition_task("T-1", "implemented", legacy.ACTOR_EXECUTOR)
            if script == "happy":
                run.record_verdicts("T-1", "PASS", "PASS")
            elif script == "blocked":
                run.record_verdicts("T-1", "BLOCKED", "FAIL")
            elif script == "repair_then_pass":
                run.record_verdicts("T-1", "FAIL", "FAIL")
                run.begin_repair("T-1", maximum=2)
                run.transition_task("T-1", "running")
                run.transition_task("T-1", "implemented", legacy.ACTOR_EXECUTOR)
                run.record_verdicts("T-1", "PASS", "PASS")
            elif script == "repair_exhausted":
                for _ in range(2):
                    run.record_verdicts("T-1", "FAIL", "FAIL")
                    self.assertTrue(run.begin_repair("T-1", maximum=2))
                    run.transition_task("T-1", "running")
                    run.transition_task("T-1", "implemented", legacy.ACTOR_EXECUTOR)
                run.record_verdicts("T-1", "FAIL", "FAIL")
                self.assertFalse(run.begin_repair("T-1", maximum=2))
                run.transition_task("T-1", "blocked", note="repair budget spent")
            rec = run.task("T-1")
            verification = dict(rec.verification)
            verification.pop("verified_at", None)
            return rec.status, rec.attempts, verification

    def _domain_facts(self, script: str) -> tuple[str, int, dict]:
        run = _one(TaskStatus.PENDING, bound=2)
        run = apply(run, Transition("T-1", TaskStatus.READY))
        run = apply(run, Transition("T-1", TaskStatus.RUNNING))
        run = apply(run, Transition("T-1", TaskStatus.IMPLEMENTED, actor=Actor.EXECUTOR))
        if script == "happy":
            run = apply(run, RecordVerdicts("T-1", "PASS", "PASS"))
        elif script == "blocked":
            run = apply(run, RecordVerdicts("T-1", "BLOCKED", "FAIL"))
        elif script == "repair_then_pass":
            run = apply(run, RecordVerdicts("T-1", "FAIL", "FAIL"))
            run = apply(run, BeginRepair("T-1"))
            run = apply(run, Transition("T-1", TaskStatus.RUNNING))
            run = apply(run, Transition("T-1", TaskStatus.IMPLEMENTED, actor=Actor.EXECUTOR))
            run = apply(run, RecordVerdicts("T-1", "PASS", "PASS"))
        elif script == "repair_exhausted":
            for _ in range(2):
                run = apply(run, RecordVerdicts("T-1", "FAIL", "FAIL"))
                run = apply(run, BeginRepair("T-1"))
                run = apply(run, Transition("T-1", TaskStatus.RUNNING))
                run = apply(run, Transition("T-1", TaskStatus.IMPLEMENTED,
                                            actor=Actor.EXECUTOR))
            run = apply(run, RecordVerdicts("T-1", "FAIL", "FAIL"))
            with self.assertRaises(RepairBudgetExhausted):
                apply(run, BeginRepair("T-1"))
            run = apply(run, Transition("T-1", TaskStatus.BLOCKED,
                                        note="repair budget spent"))
        task = run.task("T-1")
        verification = task.verification.as_record()
        verification.pop("verified_at", None)
        return task.status.value, task.attempts, verification

    def test_scenarios_are_compatibility_equivalent(self) -> None:
        for script in ("happy", "blocked", "repair_then_pass", "repair_exhausted"):
            with self.subTest(script=script):
                self.assertEqual(self._domain_facts(script), self._legacy_facts(script))

    def test_resume_after_running_matches_pipeline_core_rollback(self) -> None:
        # legacy: RESUME_ROLLBACKS maps running -> ready
        self.assertEqual(legacy.RESUME_ROLLBACKS["running"], "ready")
        out = apply(_one(TaskStatus.RUNNING), Resume())
        self.assertEqual(out.task("T-1").status.value, "ready")

    def test_as_task_record_matches_a_freshly_migrated_taskrecord_subset(self) -> None:
        record = legacy.TaskRecord("T-1", depends_on=["T-0"])
        rendered = TaskState(id="T-1", depends_on=("T-0",)).as_task_record()
        legacy_map = dataclasses.asdict(record)
        for key in rendered:
            with self.subTest(key=key):
                self.assertEqual(rendered[key], legacy_map[key])


class RunStateHelpersTests(unittest.TestCase):
    def test_ready_tasks_needs_every_dependency_verified(self) -> None:
        run = RunState.plan("demo", [
            TaskState(id="A", status=TaskStatus.VERIFIED),
            TaskState(id="B", status=TaskStatus.PENDING, depends_on=("A",)),
            TaskState(id="C", status=TaskStatus.PENDING, depends_on=("A", "B")),
        ])
        self.assertEqual(run.ready_tasks(), ("B",))

    def test_with_task_preserves_order_and_rejects_unknown(self) -> None:
        run = _one(TaskStatus.PENDING)
        moved = run.with_task(dataclasses.replace(run.task("T-1"), status=TaskStatus.READY))
        self.assertEqual([t.id for t in moved.tasks], ["T-1"])
        with self.assertRaises(UnknownTask):
            run.with_task(TaskState(id="ZZ"))

    def test_default_run_status_is_planned(self) -> None:
        self.assertIs(RunState.plan("demo", []).status, RunStatus.PLANNED)


if __name__ == "__main__":
    unittest.main()
