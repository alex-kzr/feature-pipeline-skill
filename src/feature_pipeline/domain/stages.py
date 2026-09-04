"""Stage contracts and the immutable compiled stage sequence (PE-01).

``docs/plans/tasks/PE-01_stage-contracts-engine.md``.

The pipeline's accepted stages (SKILL.md §3) were an implicit chain of direct calls in
``pipeline_core``, with a second, disconnected generic path in ``pipeline_core.stages``. PE-01
introduces one small, explicit abstraction around the *accepted* behaviour without turning
stages into a second task runner:

* :class:`StageContext` — the read-only view a handler is given: the compiled run plan, the
  stage being run, every prior :class:`StageOutcome`, and an opaque application-supplied
  ``resources`` bundle (the domain never inspects a resource);
* :class:`StageOutcome` — the single value a handler returns: a status, a human ``detail``,
  and a small string ``evidence`` mapping;
* :class:`StageHandler` — the whole handler contract is ``run(context) -> StageOutcome``. A
  richer interface would let handlers duplicate the services they wrap and recreate two
  orchestration layers (PE-01 Risks);
* :func:`compile_stage_sequence` — turns an explicit, ordered list of
  :class:`~feature_pipeline.domain.vocabulary.StageId` plus a handler map into an immutable
  :class:`CompiledStageSequence`, and **fails closed** on a stage that has neither a handler
  nor a declared deferred capability (PE-01 AC-2). A deferred stage becomes a compile-time
  capability the engine records as ``skipped`` — never callable dead code.

The domain knows nothing about processes, the filesystem, or CLI exit codes; every rejection
is a :class:`~feature_pipeline.domain.errors.DomainError`. The engine that consumes a compiled
sequence is :class:`feature_pipeline.application.pipeline_engine.PipelineEngine`.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterator, Mapping, Protocol, Sequence, runtime_checkable

from .errors import DomainError
from .plan import CompiledRunPlan
from .vocabulary import StageId


class StageStatus(StrEnum):
    """The terminal disposition of one stage.

    ``completed`` and ``skipped`` let the sequence proceed; ``failed`` and ``blocked`` stop
    it at that stage (:attr:`StageOutcome.terminal`).
    """

    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    BLOCKED = "blocked"


_CONTINUING: frozenset[StageStatus] = frozenset(
    {StageStatus.COMPLETED, StageStatus.SKIPPED}
)


class StageError(DomainError):
    """Base class for a fail-closed stage-sequence rejection."""

    code = "stage-error"


class UnsupportedStage(StageError):
    """A requested stage has no handler and no declared deferred capability (AC-2)."""

    code = "unsupported-stage"


class DuplicateStage(StageError):
    """The same stage appears more than once in one sequence."""

    code = "duplicate-stage"


class StageSequenceError(StageError):
    """The requested stages are not in the documented order (SKILL.md §3)."""

    code = "stage-sequence-out-of-order"


class EmptyStageSequence(StageError):
    """A pipeline needs at least one stage."""

    code = "empty-stage-sequence"


@dataclass(frozen=True)
class StageOutcome:
    """Everything one stage produced, with a fail-closed disposition."""

    stage: StageId
    status: StageStatus
    detail: str = ""
    evidence: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the stage did not stop the sequence."""
        return self.status in _CONTINUING

    @property
    def terminal(self) -> bool:
        """True when the engine must stop the sequence at this stage."""
        return self.status not in _CONTINUING


@dataclass(frozen=True)
class StageContext:
    """The read-only view a :class:`StageHandler` is handed for one stage.

    ``resources`` is an opaque, application-supplied bundle (a run lifecycle, a task-execution
    request, …). The domain never looks inside it; :meth:`resource` only names a missing key
    clearly.
    """

    plan: CompiledRunPlan
    stage: StageId
    completed: tuple[StageOutcome, ...] = ()
    resources: Mapping[str, object] = field(default_factory=dict)

    def outcome(self, stage: StageId) -> StageOutcome | None:
        """The recorded outcome of an earlier ``stage`` in this run, or ``None``."""
        for done in self.completed:
            if done.stage == stage:
                return done
        return None

    def resource(self, name: str) -> object:
        """The named resource, or a :class:`KeyError` that names the requesting stage."""
        try:
            return self.resources[name]
        except KeyError:
            raise KeyError(
                f"stage {self.stage.value!r} needs resource {name!r}"
            ) from None


@runtime_checkable
class StageHandler(Protocol):
    """The complete handler contract: run one stage, return one outcome."""

    def run(self, context: StageContext) -> StageOutcome: ...


@dataclass(frozen=True)
class StageStep:
    """One slot in a compiled sequence: an available handler, or a deferred capability."""

    stage: StageId
    handler: StageHandler | None
    deferred_reason: str = ""

    @property
    def available(self) -> bool:
        return self.handler is not None


@dataclass(frozen=True)
class CompiledStageSequence:
    """An immutable, ordered, validated stage sequence — the engine's only input."""

    steps: tuple[StageStep, ...]

    def __iter__(self) -> Iterator[StageStep]:
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    @property
    def stages(self) -> tuple[StageId, ...]:
        return tuple(step.stage for step in self.steps)

    @property
    def available(self) -> tuple[StageId, ...]:
        return tuple(step.stage for step in self.steps if step.available)

    @property
    def deferred(self) -> tuple[StageId, ...]:
        return tuple(step.stage for step in self.steps if not step.available)


def compile_stage_sequence(
    stages: Sequence[StageId],
    handlers: Mapping[StageId, StageHandler],
    *,
    deferred: Mapping[StageId, str] | None = None,
) -> CompiledStageSequence:
    """Validate ``stages`` against ``handlers`` and return an immutable sequence.

    Fails closed, before the engine runs and before any run state is created (AC-2), on:

    * an empty ``stages`` list (:class:`EmptyStageSequence`);
    * an entry that is not a :class:`~feature_pipeline.domain.vocabulary.StageId`
      (:class:`UnsupportedStage`);
    * a repeated stage (:class:`DuplicateStage`);
    * stages out of the documented order (:class:`StageSequenceError`);
    * a stage with no handler and no ``deferred`` reason, or one that is *both* handled and
      deferred (:class:`UnsupportedStage`).
    """
    reasons = dict(deferred or {})
    if not stages:
        raise EmptyStageSequence("a pipeline needs at least one stage")

    seen: set[StageId] = set()
    last_number = 0
    steps: list[StageStep] = []
    for stage in stages:
        if not isinstance(stage, StageId):
            raise UnsupportedStage(f"not a pipeline stage: {stage!r}")
        if stage in seen:
            raise DuplicateStage(
                f"stage {stage.value!r} appears more than once in the sequence"
            )
        if stage.number <= last_number:
            raise StageSequenceError(
                f"stage {stage.value!r} is out of the documented order (SKILL.md §3)"
            )
        seen.add(stage)
        last_number = stage.number

        handler = handlers.get(stage)
        reason = reasons.get(stage)
        if handler is not None and reason:
            raise UnsupportedStage(
                f"stage {stage.value!r} is both handled and marked deferred"
            )
        if handler is not None:
            steps.append(StageStep(stage, handler))
            continue
        if not reason:
            raise UnsupportedStage(
                f"stage {stage.value!r} has no handler and is not a declared deferred "
                f"capability"
            )
        steps.append(StageStep(stage, None, reason))

    return CompiledStageSequence(tuple(steps))


__all__ = [
    "StageStatus",
    "StageError",
    "UnsupportedStage",
    "DuplicateStage",
    "StageSequenceError",
    "EmptyStageSequence",
    "StageOutcome",
    "StageContext",
    "StageHandler",
    "StageStep",
    "CompiledStageSequence",
    "compile_stage_sequence",
]
