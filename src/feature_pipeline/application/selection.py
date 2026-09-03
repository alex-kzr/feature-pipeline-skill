"""The one application entry point for task selection and the dependency gate.

``pipeline_core`` selects tasks in two hand-written places today —
``runner_cli._select`` (the dry-run plan) and ``execution._select_ids`` (execute mode) — and
computes "which dependencies are still unmet" in two more. Both selectors share the same
three arguments (``--task``, ``--through``, neither) and must agree byte-for-byte or a dry
run and its real execution diverge.

:func:`resolve_selection` is the single function both call sites will delegate to in the
CP-01 / CP-02 cutover. It runs every rejection through
:class:`feature_pipeline.domain.graph.TaskGraph` and re-labels the graph's neutral
``unknown task`` error with the **exact** historical CLI strings
(``--task names an unknown task: X`` / ``--through names an unknown task: X``), so the cutover
changes no accepted input and no message.

:func:`dependency_status` answers "is this task's dependency gate open?" as typed data — a
:class:`DependencyStatus`, never a process exit code. A caller turns a closed gate into
``ResultMapper.blocked(...)`` (exit ``20``); the domain never learns the number.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

from feature_pipeline.domain.graph import TaskGraph, UnknownTask


@dataclass(frozen=True)
class ResolvedSelection:
    """The tasks one invocation selected, plus how the caller asked for them."""

    #: Selected task ids. ``--task`` → exactly one; ``--through`` → the plan-order prefix;
    #: neither → every task in authored plan order (byte-identical to the shipped selectors).
    task_ids: tuple[str, ...]
    #: ``"task"`` | ``"through"`` | ``"all"``.
    mode: str


@dataclass(frozen=True)
class DependencyStatus:
    """Whether a task's direct dependencies are all satisfied, and which are not."""

    task_id: str
    ready: bool
    #: Unmet direct dependencies in declared edge order (empty when :attr:`ready`).
    unmet: tuple[str, ...]


class SelectionError(ValueError):
    """``--task`` and ``--through`` were both given — mutually exclusive at the CLI."""


def resolve_selection(
    graph: TaskGraph, *, task: str | None = None, through: str | None = None
) -> ResolvedSelection:
    """Resolve ``--task`` / ``--through`` / neither against ``graph``.

    Raises :class:`SelectionError` if both are given,
    :class:`~feature_pipeline.domain.graph.UnknownTask` (message matched to the historical
    CLI string) for an unknown id, and
    :class:`~feature_pipeline.domain.graph.DependencyClosureViolation` if a ``--through``
    prefix would drop a dependency of a task it selected.
    """
    if task is not None and through is not None:
        raise SelectionError("--task and --through are mutually exclusive")
    if task is not None:
        try:
            return ResolvedSelection(graph.select_task(task), "task")
        except UnknownTask:
            raise UnknownTask(f"--task names an unknown task: {task}") from None
    if through is not None:
        try:
            return ResolvedSelection(graph.select_through(through), "through")
        except UnknownTask:
            raise UnknownTask(f"--through names an unknown task: {through}") from None
    return ResolvedSelection(graph.ids, "all")


def dependency_status(
    graph: TaskGraph, task_id: str, *, satisfied: frozenset[str]
) -> DependencyStatus:
    """Report ``task_id``'s dependency gate against the already-``satisfied`` set
    (``verified`` ∪ attested). Pure data — the caller owns the exit code."""
    unmet = graph.unmet_dependencies(task_id, satisfied=satisfied)
    return DependencyStatus(task_id, not unmet, unmet)


__all__ = [
    "ResolvedSelection",
    "DependencyStatus",
    "SelectionError",
    "resolve_selection",
    "dependency_status",
]
