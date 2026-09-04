"""Typed durable state and pure lifecycle transitions.

**DS-01** (``docs/plans/tasks/DS-01_typed-state-transitions.md``, Phase 5 of
``docs/plans/2026-09-03-feature-pipeline-refactor.md``).

``pipeline_core.state`` currently mixes three concerns in one module: the lifecycle *policy*
(which task transition is legal, who may cause it, when a repair attempt is spent), the
*serialization* shape (``schema_version``, ``asdict`` dicts, ``_empty_*`` manifests), and the
*persistence* mechanism (``Path``, ``write_json_atomic``, leases, ``os.kill``). This module
extracts the first concern only.

Everything here is:

* **Typed** — a closed vocabulary (:mod:`feature_pipeline.domain.vocabulary`) and frozen value
  objects for run, task, command, verification, repair, promotion, attestation, launch, and
  evidence state. No ``Any``-shaped mapping decides a transition.
* **Pure** — every transition is ``(RunState, Event) -> RunState``. It reads no clock, opens
  no file, and imports nothing from :mod:`pathlib`, :mod:`os`, or :mod:`json` (AC-1). A
  timestamp a record needs is carried *on the event*, resolved by the caller at the boundary
  (the :class:`feature_pipeline.ports.clock.Clock` port from DF-02).
* **Fail-closed** — an impossible transition, an unauthorized actor, an unknown task, an
  unknown verdict, or an exhausted repair budget raises a typed
  :class:`~feature_pipeline.domain.errors.DomainError` subclass with a stable ``code``
  (AC-2). Nothing is mutated on the way out.

The transition table, the actor-authorization rules, and the resume rollbacks are re-declared
here rather than imported from ``pipeline_core.state`` so the domain owns its policy; parity
with the live constants is pinned by ``tests/test_domain_states.py`` (AC-3).

The schema-v2 rendering adapters (:meth:`Verification.as_record`, :meth:`Evidence.as_record`,
:meth:`TaskState.as_task_record`) stay until DS-02/DS-03 replace the serialization and
persistence layers; a migrated call site round-trips through them unchanged.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Union

from .errors import DomainError
from .repair import RepairBound
from .vocabulary import Actor, RunStatus, TaskStatus, Verdict

# ==========================================================================================
# Policy — re-declared here, parity-pinned against ``pipeline_core.state`` in the tests.
# ==========================================================================================

#: The only task transitions the lifecycle allows. ``verified`` is terminal: it has no
#: outgoing edge. Mirrors ``pipeline_core.state.TASK_TRANSITIONS``.
TASK_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.READY, TaskStatus.BLOCKED}),
    TaskStatus.READY: frozenset({TaskStatus.RUNNING, TaskStatus.BLOCKED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.IMPLEMENTED, TaskStatus.BLOCKED}),
    TaskStatus.IMPLEMENTED: frozenset(
        {TaskStatus.VERIFIED, TaskStatus.VERIFICATION_FAILED, TaskStatus.BLOCKED}
    ),
    TaskStatus.VERIFICATION_FAILED: frozenset(
        {TaskStatus.REPAIRING, TaskStatus.IMPLEMENTED, TaskStatus.BLOCKED}
    ),
    TaskStatus.REPAIRING: frozenset(
        {TaskStatus.IMPLEMENTED, TaskStatus.BLOCKED, TaskStatus.RUNNING}
    ),
    TaskStatus.VERIFIED: frozenset(),
    TaskStatus.BLOCKED: frozenset({TaskStatus.READY, TaskStatus.IMPLEMENTED}),
}

#: Terminal task states — no transition leaves them.
TERMINAL_TASK_STATES: frozenset[TaskStatus] = frozenset(
    status for status, targets in TASK_TRANSITIONS.items() if not targets
)

#: Interrupted non-terminal states rolled back on resume. Mirrors
#: ``pipeline_core.state.RESUME_ROLLBACKS``: a ``running`` task lost its executor window and
#: returns to ``ready``; a ``repairing`` task lost its repair window and returns to
#: ``verification_failed``. Every other state resumes as-is.
RESUME_ROLLBACKS: Mapping[TaskStatus, TaskStatus] = {
    TaskStatus.RUNNING: TaskStatus.READY,
    TaskStatus.REPAIRING: TaskStatus.VERIFICATION_FAILED,
}

#: Only an executor or the runner may drive a task to ``implemented``.
_IMPLEMENTED_ACTORS: frozenset[Actor] = frozenset({Actor.RUNNER, Actor.EXECUTOR})


# ==========================================================================================
# Typed failures — module-local, like ``domain.graph`` (scope excludes ``domain/errors.py``).
# ==========================================================================================


class StateTransitionError(DomainError):
    """Base class for a rejected lifecycle transition."""

    code = "state-transition-error"


class IllegalTransition(StateTransitionError):
    """The requested target is not reachable from the task's current status."""

    code = "illegal-transition"


class UnauthorizedTransition(StateTransitionError):
    """The acting role may not cause this transition."""

    code = "unauthorized-transition"


class UnknownTask(StateTransitionError):
    """The event names a task the run does not contain."""

    code = "unknown-task"


class UnknownVerdict(StateTransitionError):
    """A verdict token is not one of ``PASS`` / ``FAIL`` / ``BLOCKED``."""

    code = "unknown-verdict"


class RepairBudgetExhausted(StateTransitionError):
    """A repair was requested for a task that has already spent every attempt."""

    code = "repair-budget-exhausted"


# ==========================================================================================
# Value objects — frozen, no I/O, no paths.
# ==========================================================================================


@dataclass(frozen=True)
class CommandRecord:
    """One recorded command invocation. Pure evidence — the runner owns capture."""

    id: str
    stage: str
    cwd: str
    argv: tuple[str, ...]
    exit_code: object
    duration: float
    stdout: str
    stderr: str


@dataclass(frozen=True)
class LaunchGenerations:
    """Per-role, monotonic launch-generation counters (next value to hand out)."""

    executor: int = 1
    task_verifier: int = 1
    test_verifier: int = 1

    _ROLES = ("executor", "task_verifier", "test_verifier")

    def consume(self, role: str) -> tuple[int, "LaunchGenerations"]:
        """Return the current generation for ``role`` and the counters advanced by one."""
        if role not in self._ROLES:
            raise DomainError(f"unknown launch role '{role}'", code="unknown-launch-role")
        current: int = getattr(self, role)
        return current, replace(self, **{role: current + 1})


@dataclass(frozen=True)
class LaunchFailure:
    """Durable evidence that one launch attempt failed and consumed its generation."""

    stage: str
    generation: int
    exit_code: object
    detail: str
    at: str | None = None


@dataclass(frozen=True)
class Promotion:
    """A task carried forward from a prior, already-closed run."""

    from_run: str
    authorized_by: str

    def as_record(self) -> dict[str, str]:
        return {"from_run": self.from_run, "authorized_by": self.authorized_by}


@dataclass(frozen=True)
class Attestation:
    """A dependency proved from another run instead of dispatched in this one."""

    dep_id: str
    source_feature: str
    source_run_id: str | None = None
    source_digest: str | None = None
    task_verdict: str | None = None
    test_verdict: str | None = None
    verified_at: str | None = None
    attested_at: str | None = None

    def as_record(self) -> dict[str, object]:
        return {
            "dep_id": self.dep_id,
            "source_feature": self.source_feature,
            "source_run_id": self.source_run_id,
            "source_digest": self.source_digest,
            "task_verdict": self.task_verdict,
            "test_verdict": self.test_verdict,
            "verified_at": self.verified_at,
            "attested_at": self.attested_at,
        }


@dataclass(frozen=True)
class Verification:
    """The two independent verifier verdicts and when they settled."""

    task_verdict: Verdict | None = None
    test_verdict: Verdict | None = None
    verified_at: str | None = None

    def as_record(self) -> dict[str, object]:
        """The ``pipeline_core.state._empty_verification`` / recorded shape."""
        return {
            "task_verdict": self.task_verdict.value if self.task_verdict else None,
            "test_verdict": self.test_verdict.value if self.test_verdict else None,
            "verified_at": self.verified_at,
        }


@dataclass(frozen=True)
class Implementation:
    """The executor-window delta attributed to one launch."""

    state: str = "not-attempted"
    changed_files: tuple[str, ...] = ()
    manifest: str | None = None
    diff: str | None = None

    def as_record(self) -> dict[str, object]:
        return {
            "state": self.state,
            "changed_files": list(self.changed_files),
            "manifest": self.manifest,
            "diff": self.diff,
        }


@dataclass(frozen=True)
class Evidence:
    """Execution evidence for a task — unavailable attribution is stated, not implied."""

    attempt: int = 0
    executor_report: str | None = None
    implementation: Implementation = field(default_factory=Implementation)

    def as_record(self) -> dict[str, object]:
        """The ``pipeline_core.state._empty_execution_evidence`` shape."""
        return {
            "attempt": self.attempt,
            "executor_report": self.executor_report,
            "implementation": self.implementation.as_record(),
        }


@dataclass(frozen=True)
class Repair:
    """The bounded repair-loop state of one task."""

    attempts: int = 0
    bound: RepairBound = field(default_factory=RepairBound.default)

    @property
    def remaining(self) -> int:
        return max(0, int(self.bound) - self.attempts)

    @property
    def exhausted(self) -> bool:
        return self.attempts >= int(self.bound)


@dataclass(frozen=True)
class TaskState:
    """One task's durable lifecycle facts — identity, status, evidence, verdicts."""

    id: str
    status: TaskStatus = TaskStatus.PENDING
    depends_on: tuple[str, ...] = ()
    blocker: str | None = None
    verification: Verification = field(default_factory=Verification)
    evidence: Evidence = field(default_factory=Evidence)
    repair: Repair = field(default_factory=Repair)
    launch: LaunchGenerations = field(default_factory=LaunchGenerations)
    launch_failures: tuple[LaunchFailure, ...] = ()
    promotion: Promotion | None = None
    attested_dependencies: tuple[Attestation, ...] = ()

    @property
    def attempts(self) -> int:
        """Repair attempts spent — the ``pipeline_core.state.TaskRecord.attempts`` field."""
        return self.repair.attempts

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATES

    def as_task_record(self) -> dict[str, object]:
        """The subset of the schema-v2 ``TaskRecord`` mapping this domain state owns.

        DS-02/DS-03 replace serialization and persistence; until then a migrated call site
        renders a compatible ``run.json`` task entry through this adapter.
        """
        return {
            "id": self.id,
            "status": self.status.value,
            "depends_on": list(self.depends_on),
            "attempts": self.repair.attempts,
            "blocker": self.blocker,
            "verification": self.verification.as_record(),
            "execution_evidence": self.evidence.as_record(),
            "promotion": self.promotion.as_record() if self.promotion else None,
            "attested_dependencies": [a.as_record() for a in self.attested_dependencies],
            "next_executor_launch_generation": self.launch.executor,
            "next_task_verifier_launch_generation": self.launch.task_verifier,
            "next_test_verifier_launch_generation": self.launch.test_verifier,
        }


@dataclass(frozen=True)
class RunState:
    """A run's typed lifecycle state — the tasks and where the run is pointing."""

    feature: str
    status: RunStatus = RunStatus.PLANNED
    tasks: tuple[TaskState, ...] = ()
    current_task: str | None = None
    commands: tuple[CommandRecord, ...] = ()

    @classmethod
    def plan(cls, feature: str, tasks: Iterable[TaskState]) -> "RunState":
        return cls(feature=feature, tasks=tuple(tasks))

    def task(self, task_id: str) -> TaskState:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise UnknownTask(f"unknown task '{task_id}'")

    def with_task(self, task: TaskState) -> "RunState":
        """Return a copy with ``task`` replacing the same-id entry (order preserved)."""
        self.task(task.id)
        return replace(
            self,
            tasks=tuple(task if t.id == task.id else t for t in self.tasks),
        )

    def ready_tasks(self) -> tuple[str, ...]:
        """Pending tasks whose every dependency has reached ``verified``."""
        verified = {t.id for t in self.tasks if t.status is TaskStatus.VERIFIED}
        return tuple(
            t.id
            for t in self.tasks
            if t.status is TaskStatus.PENDING and set(t.depends_on) <= verified
        )


# ==========================================================================================
# Events — the closed set a transition accepts.
# ==========================================================================================


@dataclass(frozen=True)
class Transition:
    """Move one task to ``to``, caused by ``actor``."""

    task_id: str
    to: TaskStatus
    actor: Actor = Actor.RUNNER
    note: str | None = None


@dataclass(frozen=True)
class RecordVerdicts:
    """Settle a task from its two independent verifier verdicts.

    Both ``PASS`` -> ``verified``. Any ``BLOCKED`` -> ``blocked`` (an external cause; no
    repair attempt is spent) and takes precedence over ``FAIL``. Otherwise
    -> ``verification_failed`` for the repair loop.
    """

    task_id: str
    task_verdict: Verdict | str
    test_verdict: Verdict | str
    at: str | None = None


@dataclass(frozen=True)
class BeginRepair:
    """Spend one repair attempt: ``verification_failed`` -> ``repairing``.

    Raises :class:`RepairBudgetExhausted` when the task has already spent every attempt, so
    the caller blocks it deterministically instead of silently looping.
    """

    task_id: str


@dataclass(frozen=True)
class Resume:
    """Roll every interrupted task back through :data:`RESUME_ROLLBACKS`."""


Event = Union[Transition, RecordVerdicts, BeginRepair, Resume]


# ==========================================================================================
# The pure transition — ``(RunState, Event) -> RunState``.
# ==========================================================================================


def apply(state: RunState, event: Event) -> RunState:
    """Apply one typed ``event`` to ``state`` and return the next state.

    Pure: no clock, no filesystem, no mutation of ``state`` (every value object is frozen).
    A rejected event raises a :class:`~feature_pipeline.domain.errors.DomainError` subclass
    and leaves ``state`` untouched.
    """
    if isinstance(event, Transition):
        return _apply_transition(state, event)
    if isinstance(event, RecordVerdicts):
        return _apply_verdicts(state, event)
    if isinstance(event, BeginRepair):
        return _apply_begin_repair(state, event)
    if isinstance(event, Resume):
        return _apply_resume(state)
    raise DomainError(f"unknown state event: {type(event).__name__!r}", code="unknown-event")


def _check_transition(task: TaskState, to: TaskStatus, actor: Actor) -> None:
    """Raise if ``task`` may not move to ``to`` under ``actor``; return silently otherwise."""
    if to not in TASK_TRANSITIONS.get(task.status, frozenset()):
        raise IllegalTransition(
            f"cannot transition {task.id} from {task.status.value} to {to.value}"
        )
    if to is TaskStatus.IMPLEMENTED and actor not in _IMPLEMENTED_ACTORS:
        raise UnauthorizedTransition("only an executor or runner may mark implemented")
    if to is TaskStatus.VERIFIED and actor is not Actor.RUNNER:
        raise UnauthorizedTransition("only the runner may mark verified")
    if task.status is TaskStatus.BLOCKED and actor is not Actor.HUMAN:
        raise UnauthorizedTransition("only a human may unblock a task")


def _apply_transition(state: RunState, event: Transition) -> RunState:
    task = state.task(event.task_id)
    _check_transition(task, event.to, event.actor)
    moved = replace(task, status=event.to)
    if event.to is TaskStatus.BLOCKED and event.note:
        moved = replace(moved, blocker=event.note)
    return state.with_task(moved)


def _coerce_verdict(value: Verdict | str) -> Verdict:
    if isinstance(value, Verdict):
        return value
    try:
        return Verdict(value)
    except ValueError:
        raise UnknownVerdict(
            f"'{value}' is not a verdict token; expected one of "
            f"{', '.join(v.value for v in Verdict)}"
        ) from None


def _apply_verdicts(state: RunState, event: RecordVerdicts) -> RunState:
    task = state.task(event.task_id)
    task_verdict = _coerce_verdict(event.task_verdict)
    test_verdict = _coerce_verdict(event.test_verdict)
    verification = Verification(task_verdict, test_verdict, event.at)

    if Verdict.BLOCKED in (task_verdict, test_verdict):
        reason = "a verifier returned BLOCKED — external cause"
        settled = replace(task, verification=verification)
        if settled.status is not TaskStatus.BLOCKED:
            _check_transition(settled, TaskStatus.BLOCKED, Actor.RUNNER)
            settled = replace(settled, status=TaskStatus.BLOCKED)
        settled = replace(settled, blocker=reason)
        return state.with_task(settled)

    target = (
        TaskStatus.VERIFIED
        if task_verdict is test_verdict is Verdict.PASS
        else TaskStatus.VERIFICATION_FAILED
    )
    _check_transition(task, target, Actor.RUNNER)
    return state.with_task(replace(task, status=target, verification=verification))


def _apply_begin_repair(state: RunState, event: BeginRepair) -> RunState:
    task = state.task(event.task_id)
    if task.status is not TaskStatus.VERIFICATION_FAILED:
        raise IllegalTransition(
            f"cannot begin a repair for {task.id} from {task.status.value}"
        )
    if task.repair.exhausted:
        raise RepairBudgetExhausted(
            f"{task.id} has consumed all {int(task.repair.bound)} repair attempts"
        )
    spent = replace(task.repair, attempts=task.repair.attempts + 1)
    return state.with_task(replace(task, repair=spent, status=TaskStatus.REPAIRING))


def _apply_resume(state: RunState) -> RunState:
    rolled = tuple(
        replace(task, status=RESUME_ROLLBACKS[task.status])
        if task.status in RESUME_ROLLBACKS
        else task
        for task in state.tasks
    )
    return replace(state, tasks=rolled)


__all__ = [
    # policy
    "TASK_TRANSITIONS",
    "TERMINAL_TASK_STATES",
    "RESUME_ROLLBACKS",
    # failures
    "StateTransitionError",
    "IllegalTransition",
    "UnauthorizedTransition",
    "UnknownTask",
    "UnknownVerdict",
    "RepairBudgetExhausted",
    # value objects
    "CommandRecord",
    "LaunchGenerations",
    "LaunchFailure",
    "Promotion",
    "Attestation",
    "Verification",
    "Implementation",
    "Evidence",
    "Repair",
    "TaskState",
    "RunState",
    # events + transition
    "Transition",
    "RecordVerdicts",
    "BeginRepair",
    "Resume",
    "Event",
    "apply",
]
