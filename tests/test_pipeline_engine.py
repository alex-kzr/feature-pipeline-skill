"""PE-01 — small stage contracts and one explicit :class:`PipelineEngine`.

The pipeline's accepted stages (SKILL.md §3) were only ever an implicit sequence of direct
calls in ``pipeline_core``, with a disconnected generic alternative in
``pipeline_core.stages``. PE-01 puts the accepted stages behind one small, explicit
abstraction:

* ``StageContext`` / ``StageOutcome`` / ``StageHandler`` are the whole contract — a handler
  receives a read-only context and returns one outcome, nothing more (the "rich handler
  interface" is the named risk);
* :func:`compile_stage_sequence` turns an explicit ordered list of
  :class:`~feature_pipeline.domain.vocabulary.StageId` plus a handler map into an immutable
  :class:`CompiledStageSequence`, and **fails closed** on a stage with no handler and no
  declared deferred capability (AC-2 — before any run state exists);
* :class:`PipelineEngine` iterates that compiled sequence, threading each outcome into the
  next context and stopping at the first terminal outcome — a fake stage is inserted by
  adding an entry to the handler map, with no change to the engine or the CLI (AC-1);
* a deferred stage is a compile-time capability that the engine records as ``skipped`` — it
  is never callable dead code;
* :class:`TaskExecutionStage` wraps :class:`~feature_pipeline.application.task_engine.TaskEngine`
  without copying any of its policy, and its terminal ``verified`` / ``blocked`` maps onto
  the same run-level exit code the CLI produces today (AC-3).

Standard library only.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field

from feature_pipeline.application.pipeline_engine import (
    CallableStage,
    PipelineEngine,
    PipelineRun,
    TaskExecutionStage,
)
from feature_pipeline.application.results import Outcome
from feature_pipeline.domain.identifiers import FeatureId
from feature_pipeline.domain.plan import CompiledRunPlan
from feature_pipeline.domain.stages import (
    CompiledStageSequence,
    DuplicateStage,
    EmptyStageSequence,
    StageContext,
    StageOutcome,
    StageSequenceError,
    StageStatus,
    UnsupportedStage,
    compile_stage_sequence,
)
from feature_pipeline.domain.vocabulary import StageId


def _plan() -> CompiledRunPlan:
    """A minimal compiled plan — the engine only ever passes it through to handlers."""
    return CompiledRunPlan(
        feature=FeatureId("demo-feature"),
        project="feature-pipeline",
        selection_mode="through",
        selection=("T-1",),
        order=("T-1",),
        tasks=(),
        adapter="claude",
        controls=(),
    )


@dataclass
class _RecordingStage:
    """A fake handler: records that it ran and returns a configured outcome."""

    stage: StageId
    status: StageStatus = StageStatus.COMPLETED
    detail: str = ""
    log: list[str] = field(default_factory=list)
    seen_prior: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, context: StageContext) -> StageOutcome:
        self.log.append(context.stage.value)
        self.seen_prior.append(tuple(o.stage.value for o in context.completed))
        return StageOutcome(context.stage, self.status, detail=self.detail)


class CompileStageSequenceTests(unittest.TestCase):
    def test_compiles_available_and_deferred_steps_in_order(self) -> None:
        impl = _RecordingStage(StageId.IMPLEMENTATION)
        sequence = compile_stage_sequence(
            [StageId.TASK_SELECTION, StageId.IMPLEMENTATION, StageId.DOCUMENTATION],
            {StageId.TASK_SELECTION: _RecordingStage(StageId.TASK_SELECTION),
             StageId.IMPLEMENTATION: impl},
            deferred={StageId.DOCUMENTATION: "PE-02 activates stages 10-16"},
        )
        self.assertIsInstance(sequence, CompiledStageSequence)
        self.assertEqual(
            sequence.stages,
            (StageId.TASK_SELECTION, StageId.IMPLEMENTATION, StageId.DOCUMENTATION),
        )
        self.assertEqual(sequence.available, (StageId.TASK_SELECTION, StageId.IMPLEMENTATION))
        self.assertEqual(sequence.deferred, (StageId.DOCUMENTATION,))

    def test_unsupported_stage_fails_closed_at_compile_time(self) -> None:
        # AC-2: a stage with neither a handler nor a declared deferred capability is rejected
        # by compilation, long before any run state is created.
        with self.assertRaises(UnsupportedStage) as caught:
            compile_stage_sequence(
                [StageId.TASK_SELECTION, StageId.VERIFICATION],
                {StageId.TASK_SELECTION: _RecordingStage(StageId.TASK_SELECTION)},
            )
        self.assertEqual(caught.exception.code, "unsupported-stage")

    def test_handler_and_deferred_for_the_same_stage_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedStage):
            compile_stage_sequence(
                [StageId.IMPLEMENTATION],
                {StageId.IMPLEMENTATION: _RecordingStage(StageId.IMPLEMENTATION)},
                deferred={StageId.IMPLEMENTATION: "cannot be both"},
            )

    def test_duplicate_stage_is_rejected(self) -> None:
        with self.assertRaises(DuplicateStage):
            compile_stage_sequence(
                [StageId.IMPLEMENTATION, StageId.IMPLEMENTATION],
                {StageId.IMPLEMENTATION: _RecordingStage(StageId.IMPLEMENTATION)},
            )

    def test_out_of_order_sequence_is_rejected(self) -> None:
        with self.assertRaises(StageSequenceError):
            compile_stage_sequence(
                [StageId.VERIFICATION, StageId.IMPLEMENTATION],
                {StageId.VERIFICATION: _RecordingStage(StageId.VERIFICATION),
                 StageId.IMPLEMENTATION: _RecordingStage(StageId.IMPLEMENTATION)},
            )

    def test_empty_sequence_is_rejected(self) -> None:
        with self.assertRaises(EmptyStageSequence):
            compile_stage_sequence([], {})

    def test_non_stage_identity_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedStage):
            compile_stage_sequence(["implementation"], {})  # type: ignore[list-item]


class StageValueObjectTests(unittest.TestCase):
    def test_stage_outcome_ok_and_terminal_are_complementary(self) -> None:
        for status, ok in (
            (StageStatus.COMPLETED, True),
            (StageStatus.SKIPPED, True),
            (StageStatus.FAILED, False),
            (StageStatus.BLOCKED, False),
        ):
            outcome = StageOutcome(StageId.PLAN, status)
            self.assertEqual(outcome.ok, ok)
            self.assertEqual(outcome.terminal, not ok)

    def test_compiled_sequence_len_and_context_prior_lookup(self) -> None:
        sequence = compile_stage_sequence(
            [StageId.TASK_SELECTION, StageId.IMPLEMENTATION],
            {StageId.TASK_SELECTION: _RecordingStage(StageId.TASK_SELECTION),
             StageId.IMPLEMENTATION: _RecordingStage(StageId.IMPLEMENTATION)},
        )
        self.assertEqual(len(sequence), 2)

        select = StageOutcome(StageId.TASK_SELECTION, StageStatus.COMPLETED, detail="picked T-1")
        context = StageContext(
            plan=_plan(), stage=StageId.IMPLEMENTATION, completed=(select,))
        self.assertIs(context.outcome(StageId.TASK_SELECTION), select)
        self.assertIsNone(context.outcome(StageId.VERIFICATION))


class PipelineEngineTests(unittest.TestCase):
    def test_runs_every_available_stage_in_compiled_order(self) -> None:
        calls: list[str] = []
        select = _RecordingStage(StageId.TASK_SELECTION, log=calls)
        impl = _RecordingStage(StageId.IMPLEMENTATION, log=calls)
        engine = PipelineEngine(
            compile_stage_sequence(
                [StageId.TASK_SELECTION, StageId.IMPLEMENTATION],
                {StageId.TASK_SELECTION: select, StageId.IMPLEMENTATION: impl},
            )
        )

        run = engine.run(_plan())

        self.assertIsInstance(run, PipelineRun)
        self.assertEqual(calls, ["task-selection", "implementation"])
        self.assertEqual(
            tuple(o.stage for o in run.outcomes),
            (StageId.TASK_SELECTION, StageId.IMPLEMENTATION),
        )
        self.assertTrue(run.ok)
        self.assertEqual(run.result.outcome, Outcome.GATE_PENDING)
        self.assertEqual(run.exit_code, 10)  # AC-3: a clean run is still "gate pending"

    def test_context_threads_prior_outcomes(self) -> None:
        impl = _RecordingStage(StageId.IMPLEMENTATION)
        engine = PipelineEngine(
            compile_stage_sequence(
                [StageId.TASK_SELECTION, StageId.IMPLEMENTATION],
                {StageId.TASK_SELECTION: _RecordingStage(StageId.TASK_SELECTION),
                 StageId.IMPLEMENTATION: impl},
            )
        )

        engine.run(_plan())

        self.assertEqual(impl.seen_prior, [("task-selection",)])

    def test_fake_stage_inserts_without_touching_engine_or_cli(self) -> None:
        # AC-1: inserting a handler for an accepted StageId is the only change needed.
        probe: list[str] = []
        handlers = {
            StageId.TASK_SELECTION: _RecordingStage(StageId.TASK_SELECTION, log=probe),
            StageId.EXECUTOR_ROUTING: _RecordingStage(StageId.EXECUTOR_ROUTING, log=probe),
            StageId.IMPLEMENTATION: _RecordingStage(StageId.IMPLEMENTATION, log=probe),
        }
        engine = PipelineEngine(
            compile_stage_sequence(list(handlers), handlers)
        )

        engine.run(_plan())

        self.assertEqual(probe, ["task-selection", "executor-routing", "implementation"])

    def test_first_failed_stage_short_circuits_with_exit_30(self) -> None:
        after = _RecordingStage(StageId.IMPLEMENTATION)
        engine = PipelineEngine(
            compile_stage_sequence(
                [StageId.TASK_SELECTION, StageId.IMPLEMENTATION],
                {StageId.TASK_SELECTION: _RecordingStage(
                    StageId.TASK_SELECTION, status=StageStatus.FAILED, detail="unroutable"),
                 StageId.IMPLEMENTATION: after},
            )
        )

        run = engine.run(_plan())

        self.assertEqual(after.log, [])  # never reached
        self.assertFalse(run.ok)
        self.assertEqual(run.result.outcome, Outcome.ERROR)
        self.assertEqual(run.exit_code, 30)
        self.assertEqual(run.terminal_outcome.detail, "unroutable")

    def test_blocked_stage_short_circuits_with_exit_20(self) -> None:
        after = _RecordingStage(StageId.VERIFICATION)
        engine = PipelineEngine(
            compile_stage_sequence(
                [StageId.IMPLEMENTATION, StageId.VERIFICATION],
                {StageId.IMPLEMENTATION: _RecordingStage(
                    StageId.IMPLEMENTATION, status=StageStatus.BLOCKED, detail="held lease"),
                 StageId.VERIFICATION: after},
            )
        )

        run = engine.run(_plan())

        self.assertEqual(after.log, [])
        self.assertEqual(run.result.outcome, Outcome.BLOCKED)
        self.assertEqual(run.exit_code, 20)

    def test_deferred_stage_is_recorded_skipped_never_called(self) -> None:
        tail = _RecordingStage(StageId.REPAIR)
        engine = PipelineEngine(
            compile_stage_sequence(
                [StageId.IMPLEMENTATION, StageId.VERIFICATION, StageId.REPAIR],
                {StageId.IMPLEMENTATION: _RecordingStage(StageId.IMPLEMENTATION),
                 StageId.REPAIR: tail},
                deferred={StageId.VERIFICATION: "stages 10-16 need separate feature approval"},
            )
        )

        run = engine.run(_plan())

        deferred = run.outcomes[1]
        self.assertEqual(deferred.stage, StageId.VERIFICATION)
        self.assertEqual(deferred.status, StageStatus.SKIPPED)
        self.assertIn("separate feature approval", deferred.detail)
        self.assertEqual(tail.log, ["repair"])  # downstream still runs
        self.assertTrue(run.ok)

    def test_callable_stage_adapter_wraps_a_plain_function(self) -> None:
        engine = PipelineEngine(
            compile_stage_sequence(
                [StageId.PLAN],
                {StageId.PLAN: CallableStage(
                    lambda ctx: StageOutcome(ctx.stage, StageStatus.COMPLETED, detail="ok"))},
            )
        )

        run = engine.run(_plan())

        self.assertEqual(run.outcomes[0].detail, "ok")


class _FakeTaskEngine:
    def __init__(self, result: object) -> None:
        self._result = result
        self.calls: list[tuple[object, object]] = []

    def run(self, life: object, request: object) -> object:
        self.calls.append((life, request))
        return self._result


@dataclass(frozen=True)
class _FakeTaskRunResult:
    task_id: str
    status: str
    attempts: int = 0
    gates: int = 1
    blocker: str | None = None


class TaskExecutionStageTests(unittest.TestCase):
    def test_delegates_the_whole_task_lifecycle_to_task_engine(self) -> None:
        result = _FakeTaskRunResult("T-1", "verified", attempts=0, gates=1)
        fake = _FakeTaskEngine(result)
        stage = TaskExecutionStage(fake)  # type: ignore[arg-type]
        life, request = object(), object()

        context = StageContext(
            plan=_plan(),
            stage=StageId.IMPLEMENTATION,
            resources={"lifecycle": life, "task_execution": request},
        )
        outcome = stage.run(context)

        self.assertEqual(fake.calls, [(life, request)])  # delegated once, verbatim
        self.assertEqual(outcome.stage, StageId.IMPLEMENTATION)
        self.assertEqual(outcome.status, StageStatus.COMPLETED)
        self.assertEqual(outcome.evidence["task_id"], "T-1")

    def test_blocked_task_maps_to_a_blocked_stage_outcome(self) -> None:
        result = _FakeTaskRunResult("T-1", "blocked", attempts=2, gates=3, blocker="deps unmet")
        stage = TaskExecutionStage(_FakeTaskEngine(result))  # type: ignore[arg-type]

        engine = PipelineEngine(
            compile_stage_sequence(
                [StageId.IMPLEMENTATION], {StageId.IMPLEMENTATION: stage})
        )
        run = engine.run(
            _plan(), resources={"lifecycle": object(), "task_execution": object()})

        self.assertEqual(run.outcomes[0].status, StageStatus.BLOCKED)
        self.assertEqual(run.outcomes[0].detail, "deps unmet")
        self.assertEqual(run.exit_code, 20)  # AC-3: same run-level exit as today

    def test_missing_resource_is_a_keyerror_naming_the_stage(self) -> None:
        stage = TaskExecutionStage(_FakeTaskEngine(None))  # type: ignore[arg-type]
        context = StageContext(plan=_plan(), stage=StageId.IMPLEMENTATION)
        with self.assertRaises(KeyError):
            stage.run(context)

    def test_an_optional_sink_resource_captures_the_raw_task_run_result(self) -> None:
        # PE-02: a caller that needs the full ``TaskRunResult`` (not just the mapped
        # ``StageOutcome`` — e.g. ``execute_run``'s ``ExecuteResult.task_results``) opts in by
        # passing a mutable list under ``"task_result_sink"``. Absent, nothing changes (the
        # earlier tests above pass no such key and still pass).
        result = _FakeTaskRunResult("T-1", "verified", attempts=1, gates=2)
        stage = TaskExecutionStage(_FakeTaskEngine(result))  # type: ignore[arg-type]
        sink: list[object] = []
        context = StageContext(
            plan=_plan(), stage=StageId.IMPLEMENTATION,
            resources={"lifecycle": object(), "task_execution": object(),
                       "task_result_sink": sink},
        )

        stage.run(context)

        self.assertEqual(sink, [result])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
