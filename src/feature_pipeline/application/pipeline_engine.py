"""One explicit engine over the accepted pipeline stages (PE-01).

``docs/plans/tasks/PE-01_stage-contracts-engine.md``.

:class:`PipelineEngine` executes a :class:`~feature_pipeline.domain.stages.CompiledStageSequence`
one step at a time: it builds a fresh :class:`~feature_pipeline.domain.stages.StageContext` for
each available stage (threading in every prior outcome), calls the handler, records the
outcome, and stops at the first ``terminal`` outcome. A deferred step is recorded as
``skipped`` and never called.

The engine holds **no policy**. It coordinates stage outcomes; the task lifecycle stays owned
by :class:`~feature_pipeline.application.task_engine.TaskEngine`, which :class:`TaskExecutionStage`
wraps without copying any of its behaviour. A fake stage is inserted purely by adding a
handler to the map passed to
:func:`~feature_pipeline.domain.stages.compile_stage_sequence` — no engine or CLI change
(PE-01 AC-1).

:class:`PipelineRun.result` maps the run onto the same :class:`Outcome` / exit code the CLI
produces today (PE-01 AC-3): a clean run is ``gate-pending`` (exit ``10``), a ``blocked``
stage is ``blocked`` (exit ``20``), a ``failed`` stage is ``error`` (exit ``30``).

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, ClassVar, Mapping, cast

from pipeline_core.lifecycle import RunLifecycle

from feature_pipeline.domain.plan import CompiledRunPlan
from feature_pipeline.domain.stages import (
    CompiledStageSequence,
    StageContext,
    StageOutcome,
    StageStatus,
)
from feature_pipeline.domain.vocabulary import StageId

from .results import Outcome, PipelineResult
from .task_engine import TaskEngine, TaskExecution, TaskRunResult

__all__ = [
    "PipelineRun",
    "PipelineEngine",
    "CallableStage",
    "TaskExecutionStage",
]

#: A terminal stage status -> the run-level outcome it produces. A run whose stages all
#: ``completed`` / ``skipped`` has no terminal status and resolves to ``gate-pending``.
_TERMINAL_OUTCOME: dict[StageStatus, Outcome] = {
    StageStatus.FAILED: Outcome.ERROR,
    StageStatus.BLOCKED: Outcome.BLOCKED,
}


@dataclass(frozen=True)
class PipelineRun:
    """The ordered outcomes of one engine run, plus the run-level result."""

    outcomes: tuple[StageOutcome, ...]

    @property
    def terminal_outcome(self) -> StageOutcome | None:
        """The stage outcome that stopped the run, or ``None`` if every stage continued."""
        for outcome in self.outcomes:
            if outcome.terminal:
                return outcome
        return None

    @property
    def ok(self) -> bool:
        """True when no stage stopped the run."""
        return self.terminal_outcome is None

    @property
    def result(self) -> PipelineResult:
        """The run mapped onto the CLI's stable outcome/exit-code vocabulary (AC-3)."""
        stopped = self.terminal_outcome
        if stopped is None:
            return PipelineResult(Outcome.GATE_PENDING, "")
        return PipelineResult(_TERMINAL_OUTCOME[stopped.status], stopped.detail)

    @property
    def exit_code(self) -> int:
        return self.result.exit_code


class PipelineEngine:
    """Execute one compiled stage sequence, threading outcomes and stopping at the first
    terminal one."""

    def __init__(self, sequence: CompiledStageSequence) -> None:
        self._sequence = sequence

    def run(
        self,
        plan: CompiledRunPlan,
        *,
        resources: Mapping[str, object] | None = None,
    ) -> PipelineRun:
        shared: Mapping[str, object] = dict(resources or {})
        outcomes: list[StageOutcome] = []
        for step in self._sequence:
            handler = step.handler
            if handler is None:
                outcomes.append(
                    StageOutcome(
                        step.stage,
                        StageStatus.SKIPPED,
                        detail=f"deferred capability: {step.deferred_reason}",
                    )
                )
                continue
            context = StageContext(
                plan=plan,
                stage=step.stage,
                completed=tuple(outcomes),
                resources=shared,
            )
            outcome = handler.run(context)
            outcomes.append(outcome)
            if outcome.terminal:
                break
        return PipelineRun(tuple(outcomes))


@dataclass(frozen=True)
class CallableStage:
    """Adapt a plain ``StageContext -> StageOutcome`` function to a
    :class:`~feature_pipeline.domain.stages.StageHandler`.

    Lets a planning gate, a completion service, or a test fake be wired in without a bespoke
    class (PE-01 AC-1).
    """

    fn: Callable[[StageContext], StageOutcome]

    def run(self, context: StageContext) -> StageOutcome:
        return self.fn(context)


@dataclass(frozen=True)
class TaskExecutionStage:
    """Stage 7-9 (implementation, verification, bounded repair) as one handler.

    It delegates the *entire* lifecycle of one task to :meth:`TaskEngine.run` and adds no
    policy — dispatch, the two-verifier gate, and repair all stay inside
    :class:`~feature_pipeline.application.task_engine.TaskEngine`. The handler only maps the
    terminal :class:`~feature_pipeline.application.task_engine.TaskRunResult` onto a
    :class:`~feature_pipeline.domain.stages.StageOutcome`: ``verified`` -> ``completed``, any
    other terminal state -> ``blocked`` (AC-3 — the same run-level exit the CLI emits today).

    The run lifecycle and the :class:`~feature_pipeline.application.task_engine.TaskExecution`
    request are read from the context ``resources`` under ``"lifecycle"`` and
    ``"task_execution"``; PE-02 supplies them from the compiled run plan.
    """

    stage: ClassVar[StageId] = StageId.IMPLEMENTATION

    engine: TaskEngine

    def run(self, context: StageContext) -> StageOutcome:
        life = cast(RunLifecycle, context.resource("lifecycle"))
        request = cast(TaskExecution, context.resource("task_execution"))
        result = self.engine.run(life, request)
        return _task_run_outcome(result)


def _task_run_outcome(result: TaskRunResult) -> StageOutcome:
    status = (
        StageStatus.COMPLETED
        if result.status == "verified"
        else StageStatus.BLOCKED
    )
    detail = result.blocker or f"{result.task_id} {result.status}"
    return StageOutcome(
        StageId.IMPLEMENTATION,
        status,
        detail=detail,
        evidence={
            "task_id": result.task_id,
            "attempts": str(result.attempts),
            "gates": str(result.gates),
        },
    )
