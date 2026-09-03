"""One pure task graph — dependency correctness, deterministic order, and selection.

``pipeline_core`` today re-derives the same three things in several places: the
``--task`` / ``--through`` selection (``runner_cli._select``, ``execution._select_ids``), the
"which dependencies are still unmet" line (``runner_cli`` dep-blocked, ``execution
._pending_reason``), and the readiness fixed point in ``lifecycle``. Unknown dependencies,
self-dependencies, and cycles are not checked up front at all — a bad edge surfaces late as a
``StateError`` while a run is already being built.

:class:`TaskGraph` is the single immutable value object that owns all of it:

* **construction fails closed** — a duplicate id, an edge to an unknown task, a self-edge, or
  a cycle is a typed :class:`~feature_pipeline.domain.errors.DomainError` raised before any
  caller can act on the graph (IN-02 AC-1);
* :attr:`order` is a deterministic topological sort whose tie-breaker is **plan position**,
  so a plan already authored in dependency order comes back unchanged (IN-02 AC-2);
* :meth:`select_task` / :meth:`select_through` reproduce the shipped CLI selection semantics
  exactly, and :meth:`unmet_dependencies` / :meth:`missing_dependencies` /
  :meth:`required_closure` return typed data — never a process exit code (IN-02 AC-3).

The graph knows nothing about the filesystem, processes, or CLI exit codes. The one
application entry point that maps its rejections onto the historical CLI strings is
:mod:`feature_pipeline.application.selection`; nothing here is wired into a production caller
yet — the CLI / execution de-duplication is the CP-01 / CP-02 cutover
(``docs/plans/2026-09-03-feature-pipeline-refactor.md`` Phase 4), which is why this task's
Requirements say "remove duplicate CLI/execution selection only after production callers use
the graph".

Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from heapq import heappop, heappush
from typing import Protocol

from .errors import DomainError


class _TaskLike(Protocol):
    """The shape :meth:`TaskGraph.from_definitions` needs — a
    :class:`feature_pipeline.domain.models.TaskDefinition` or a
    :class:`feature_pipeline.contracts.TaskSpec` both satisfy it."""

    @property
    def id(self) -> str: ...

    @property
    def depends_on(self) -> Sequence[str]: ...


class GraphError(DomainError):
    """Base class for every fail-closed task-graph rejection."""

    code = "graph-error"


class DuplicateTask(GraphError):
    """The same task id appears twice in the plan."""

    code = "duplicate-task"


class UnknownDependency(GraphError):
    """A task lists a ``depends_on`` id that is not itself a task in the plan."""

    code = "unknown-dependency"


class SelfDependency(GraphError):
    """A task lists itself in its own ``depends_on``."""

    code = "self-dependency"


class DependencyCycle(GraphError):
    """The dependency edges contain a cycle, so no topological order exists."""

    code = "dependency-cycle"


class UnknownTask(GraphError):
    """A selection (``--task`` / ``--through``) or a query names a task not in the plan."""

    code = "unknown-task"


class DependencyClosureViolation(GraphError):
    """A selection omits a dependency of one of the tasks it selected."""

    code = "dependency-closure-violation"


@dataclass(frozen=True)
class TaskNode:
    """One task: its id, its declared dependency edges, and its plan position."""

    id: str
    depends_on: tuple[str, ...]
    position: int


@dataclass(frozen=True)
class TaskGraph:
    """An immutable, validated dependency graph over an ordered list of tasks."""

    nodes: tuple[TaskNode, ...]

    # -- construction ------------------------------------------------------------------

    @classmethod
    def from_pairs(
        cls, pairs: Iterable[tuple[str, Sequence[str]]]
    ) -> "TaskGraph":
        """Build from ``(task_id, depends_on)`` pairs in plan order."""
        nodes = tuple(
            TaskNode(str(tid), tuple(str(dep) for dep in deps), index)
            for index, (tid, deps) in enumerate(pairs)
        )
        return cls._validated(nodes)

    @classmethod
    def from_definitions(cls, definitions: Iterable[_TaskLike]) -> "TaskGraph":
        """Build from objects exposing ``.id`` and ``.depends_on`` (e.g. a
        :class:`feature_pipeline.domain.models.TaskDefinition` or a
        :class:`feature_pipeline.contracts.TaskSpec`), in plan order."""
        return cls.from_pairs((item.id, item.depends_on) for item in definitions)

    @classmethod
    def _validated(cls, nodes: tuple[TaskNode, ...]) -> "TaskGraph":
        positions: dict[str, int] = {}
        for node in nodes:
            if node.id in positions:
                raise DuplicateTask(f"duplicate task id in plan: {node.id}")
            positions[node.id] = node.position
        for node in nodes:
            for dep in node.depends_on:
                if dep == node.id:
                    raise SelfDependency(f"{node.id} depends on itself")
                if dep not in positions:
                    raise UnknownDependency(
                        f"{node.id} depends on unknown task {dep}"
                    )
        graph = cls(nodes)
        ordered, stuck = graph._kahn()
        if stuck:
            raise DependencyCycle(
                "dependency edges form a cycle: " + " -> ".join(stuck)
            )
        return graph

    def _kahn(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """One deterministic topological pass. Returns ``(ordered, stuck)``: ``ordered`` is
        every drained id in plan-position tie-broken order; ``stuck`` is the ids that never
        drained (a cycle), in plan-position order. Exactly one of the two is non-empty for a
        validated graph — a graph that failed construction never gets here.
        """
        positions = {node.id: node.position for node in self.nodes}
        by_position = {node.position: node for node in self.nodes}
        remaining = {node.id: len(set(node.depends_on)) for node in self.nodes}
        dependents = self._dependents
        ready = sorted(pos for tid, pos in positions.items() if remaining[tid] == 0)
        ordered: list[str] = []
        while ready:
            node = by_position[heappop(ready)]
            ordered.append(node.id)
            for dependent in dependents.get(node.id, ()):
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    heappush(ready, positions[dependent])
        stuck = sorted(
            (tid for tid, count in remaining.items() if count > 0),
            key=lambda tid: positions[tid],
        )
        return tuple(ordered), tuple(stuck)

    # -- mappings ---------------------------------------------------------------------

    @property
    def _by_id(self) -> dict[str, TaskNode]:
        return {node.id: node for node in self.nodes}

    @property
    def _dependents(self) -> dict[str, tuple[str, ...]]:
        out: dict[str, list[str]] = {}
        for node in self.nodes:
            for dep in dict.fromkeys(node.depends_on):
                out.setdefault(dep, []).append(node.id)
        return {key: tuple(value) for key, value in out.items()}

    @property
    def ids(self) -> tuple[str, ...]:
        """Task ids in the order the plan authored them."""
        return tuple(node.id for node in self.nodes)

    def __contains__(self, task_id: object) -> bool:
        return isinstance(task_id, str) and task_id in self._by_id

    def _require(self, task_id: str) -> TaskNode:
        try:
            return self._by_id[task_id]
        except KeyError:
            raise UnknownTask(f"unknown task: {task_id}") from None

    def position(self, task_id: str) -> int:
        """The zero-based plan position of ``task_id``."""
        return self._require(task_id).position

    def dependencies(self, task_id: str) -> tuple[str, ...]:
        """``task_id``'s declared ``depends_on`` edges, in declared order."""
        return self._require(task_id).depends_on

    # -- deterministic order --------------------------------------------------------

    @property
    def order(self) -> tuple[str, ...]:
        """A deterministic topological order whose tie-breaker is plan position.

        Among all tasks whose dependencies are already scheduled, the one the plan listed
        earliest goes next. A plan authored in dependency order is returned unchanged; the
        documented plan-position tie-breaker is never broken by an otherwise-valid sort.
        """
        # construction already rejected any cycle, so `stuck` is always empty here
        return self._kahn()[0]

    # -- selection ------------------------------------------------------------------

    def select_task(self, task_id: str) -> tuple[str, ...]:
        """Just ``task_id`` — mirrors the CLI ``--task`` selection.

        Raises :class:`UnknownTask` if it is not a task in the plan.
        """
        self._require(task_id)
        return (task_id,)

    def select_through(self, task_id: str) -> tuple[str, ...]:
        """Every task up to and including ``task_id`` in plan order — mirrors ``--through``.

        Raises :class:`UnknownTask` if it is not a task in the plan;
        :class:`DependencyClosureViolation` if a task inside the prefix depends on a task
        the prefix leaves out (the CLI silently produced an unrunnable selection here).
        """
        cut = self._require(task_id).position + 1
        prefix = tuple(node.id for node in self.nodes if node.position < cut)
        missing = self.missing_dependencies(prefix)
        if missing:
            raise DependencyClosureViolation(
                f"--through {task_id} selects a task that depends on "
                f"{', '.join(missing)}, which the selection leaves out"
            )
        return prefix

    # -- dependency queries -------------------------------------------------------

    def required_closure(self, ids: Sequence[str]) -> frozenset[str]:
        """Every transitive dependency of ``ids`` (excluding ``ids`` themselves)."""
        seen: set[str] = set()
        stack = list(ids)
        while stack:
            current = stack.pop()
            for dep in self._require(current).depends_on:
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
        return frozenset(seen - set(ids))

    def missing_dependencies(self, selection: Sequence[str]) -> tuple[str, ...]:
        """Every task in the transitive dependency closure of ``selection`` that the
        selection itself leaves out, in plan order — the source of a
        :class:`DependencyClosureViolation`."""
        missing = self.required_closure(selection) - set(selection)
        return tuple(node.id for node in self.nodes if node.id in missing)

    def unmet_dependencies(
        self, task_id: str, *, satisfied: frozenset[str]
    ) -> tuple[str, ...]:
        """``task_id``'s direct dependencies that are not in ``satisfied``, in declared
        edge order — the same computation the CLI dep-blocked check and
        ``execution._pending_reason`` do by hand (``verified`` ∪ attested = ``satisfied``).
        """
        return tuple(
            dep for dep in self._require(task_id).depends_on if dep not in satisfied
        )


__all__ = [
    "TaskGraph",
    "TaskNode",
    "GraphError",
    "DuplicateTask",
    "UnknownDependency",
    "SelfDependency",
    "DependencyCycle",
    "UnknownTask",
    "DependencyClosureViolation",
]
