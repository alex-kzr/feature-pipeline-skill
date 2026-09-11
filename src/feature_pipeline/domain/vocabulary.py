"""Closed enumerations for the feature-pipeline domain.

Each enum replaces a set of bare string literals scattered across ``pipeline_core``. The
member *values* are byte-identical to the strings the current code compares against, so a
serialized value is explicit at its persistence boundary. ``StrEnum`` (Python 3.11+,
``requires-python >= 3.11``) keeps enum values JSON-friendly.

Parity is pinned by ``tests/test_domain_vocabulary.py`` against the live constants in
``pipeline_core.state``, ``pipeline_core.commands``, ``pipeline_core.roles``,
``pipeline_core.adapter_resolution``, ``pipeline_core.post_task``, ``pipeline_core.release``
and ``feature_pipeline.contracts``.

Standard library only.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

from .errors import InvalidTerm

_V = TypeVar("_V", bound="_Vocabulary")


class _Vocabulary(StrEnum):
    """A :class:`~enum.StrEnum` whose ``parse`` fails closed with a :class:`DomainError`."""

    @classmethod
    def parse(cls: type[_V], value: object) -> _V:
        """Return the member equal to ``value`` or raise :class:`InvalidTerm`."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                pass
        allowed = ", ".join(member.value for member in cls)
        raise InvalidTerm(
            f"{cls.__name__}: {value!r} is not one of {{{allowed}}}",
        )

    @classmethod
    def values(cls) -> tuple[str, ...]:
        """Every member value, in declaration order."""
        return tuple(member.value for member in cls)


class TaskStatus(_Vocabulary):
    """The three public product-progress states of one task."""

    TO_DO = "to_do"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class DoneResolution(_Vocabulary):
    """Human-visible resolution required for a :class:`TaskStatus.DONE` task."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"


class OperationOutcome(_Vocabulary):
    """Diagnostic outcome of an execution operation, distinct from task progress."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    RETRYABLE = "retryable"


class RunStatus(_Vocabulary):
    """Overall run status (``feature_pipeline.contracts.RUN_STATUSES``)."""

    PLANNED = "planned"
    RUNNING = "running"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"
    BLOCKED = "blocked"


class Verdict(_Vocabulary):
    """One verifier's verdict (``feature_pipeline.contracts.VERDICTS``)."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class Disposition(_Vocabulary):
    """Outcome classification of one launched command (``pipeline_core.commands``)."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class Actor(_Vocabulary):
    """Who caused a state transition (``pipeline_core.state.ACTOR_*``)."""

    RUNNER = "runner"
    EXECUTOR = "executor"
    HUMAN = "human"


class Subagent(_Vocabulary):
    """A dispatched subagent role (``feature_pipeline.contracts.SUBAGENTS``)."""

    EXECUTOR = "executor"
    TASK_VERIFIER = "task_verifier"
    TEST_VERIFIER = "test_verifier"


class AdapterName(_Vocabulary):
    """A recognised execution adapter (``pipeline_core.adapter_resolution.KNOWN_ADAPTERS``)."""

    CLAUDE = "claude"
    CODEX = "codex"


class StageId(_Vocabulary):
    """A pipeline stage in the documented, ordered sequence (SKILL.md §3).

    Stages 10–16 mirror the named constants in ``pipeline_core.post_task`` and
    ``pipeline_core.release``; :meth:`number` returns the stage's position.
    """

    PROMPT_DISCOVERY = "prompt-discovery"
    PLAN = "plan"
    PLAN_APPROVAL = "plan-approval"
    RUN_STATE = "run-state"
    TASK_SELECTION = "task-selection"
    EXECUTOR_ROUTING = "executor-routing"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    REPAIR = "repair"
    DOCUMENTATION = "documentation"
    DOCUMENTATION_AUDIT = "documentation-audit"
    GRAPHIFY_REFRESH = "graphify-refresh"
    GRAPHIFY_VERIFICATION = "graphify-verification"
    FINAL_VERIFICATION = "final-verification"
    FINAL_DIFF_APPROVAL = "final-diff-approval"
    RELEASE = "release"
    PUSH = "push"

    @property
    def number(self) -> int:
        """1-based position in the documented stage order."""
        return _STAGE_ORDER.index(self) + 1


_STAGE_ORDER: tuple[StageId, ...] = tuple(StageId)


__all__ = [
    "TaskStatus",
    "DoneResolution",
    "OperationOutcome",
    "RunStatus",
    "Verdict",
    "Disposition",
    "Actor",
    "Subagent",
    "AdapterName",
    "StageId",
]
