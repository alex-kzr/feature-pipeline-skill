"""UEI-04 — the explicit task-supersession model.

A blocked predecessor task is never rewritten to make a run go green. Instead a
later task *declares* that it supersedes that predecessor (a ``## Supersession``
section in the Markdown task file, or a ``Supersedes`` column in a plan table).
Once the declaring task is independently verified, any task that still depends on
the blocked predecessor has that one dependency satisfied by the verified
replacement — while the predecessor keeps its historical ``blocked`` record.

This module is the pure model. It:

* parses a ``Supersedes`` value into normalized task IDs;
* validates a set of declarations and **fails closed** on an invalid (unknown or
  self) declaration, a cyclic one (a two-way supersession, or a replacement that
  still depends on what it supersedes), or an ambiguous one (two different
  replacements for one predecessor);
* answers "is this dependency satisfied?" against a task-status map — a dependency
  is satisfied when it is itself ``verified`` or a verified replacement supersedes
  it.

It never mutates a task's status. A blocked predecessor stays blocked, so
supersession can never silently mark a blocked task ``verified``.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

__all__ = [
    "Supersession",
    "SupersessionError",
    "SupersessionGraph",
    "parse_supersedes",
]

_SPLIT_RE = re.compile(r"[,\s]+")
_NONE_TOKENS = {"", "-", "—", "–", "(none)", "none", "n/a"}
_VERIFIED = "verified"


class SupersessionError(Exception):
    """A supersession declaration does not meet the contract. Fails closed."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Supersession:
    """One declared edge: ``replacement`` supersedes the blocked ``superseded`` task."""

    replacement: str
    superseded: str


def parse_supersedes(raw: str | None) -> tuple[str, ...]:
    """Split a ``Supersedes`` field value into task IDs; a none-token yields ``()``."""
    if raw is None or raw.strip().lower() in _NONE_TOKENS:
        return ()
    out: list[str] = []
    for token in _SPLIT_RE.split(raw.strip()):
        cleaned = token.strip().strip("`").strip()
        if cleaned and cleaned.lower() not in _NONE_TOKENS and cleaned not in out:
            out.append(cleaned)
    return tuple(out)


def _reaches(dependencies: Mapping[str, Iterable[str]], start: str, target: str) -> bool:
    """True when ``target`` is in the transitive ``depends_on`` closure of ``start``."""
    stack = [start]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        for dep in dependencies.get(node, ()):
            if dep == target:
                return True
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return False


class SupersessionGraph:
    """An immutable, validated set of supersession edges over a known task-id set."""

    __slots__ = ("_edges", "_by_superseded")

    def __init__(
        self,
        edges: Iterable[Supersession],
        *,
        known_ids: Iterable[str],
        dependencies: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        known = set(known_ids)
        seen_pairs: set[tuple[str, str]] = set()
        by_superseded: dict[str, str] = {}
        ordered: list[Supersession] = []

        for edge in edges:
            for role, task_id in (("replacement", edge.replacement),
                                  ("superseded", edge.superseded)):
                if task_id not in known:
                    raise SupersessionError(
                        f"supersession names an unknown {role} task: {task_id}",
                        "supersession-unknown-task",
                    )
            if edge.replacement == edge.superseded:
                raise SupersessionError(
                    f"{edge.replacement} supersedes itself", "supersession-self"
                )
            pair = (edge.replacement, edge.superseded)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            existing = by_superseded.get(edge.superseded)
            if existing is not None and existing != edge.replacement:
                raise SupersessionError(
                    f"'{edge.superseded}' is superseded by both {existing} and "
                    f"{edge.replacement}",
                    "supersession-ambiguous",
                )
            by_superseded[edge.superseded] = edge.replacement
            ordered.append(edge)

        self._detect_cycle(ordered)
        if dependencies is not None:
            for edge in ordered:
                if _reaches(dependencies, edge.replacement, edge.superseded):
                    raise SupersessionError(
                        f"{edge.replacement} supersedes '{edge.superseded}' but still "
                        f"depends on it",
                        "supersession-cycle",
                    )

        self._edges = tuple(ordered)
        self._by_superseded = by_superseded

    # --- construction -------------------------------------------------------------------

    @classmethod
    def from_tasks(cls, tasks: Iterable[Mapping[str, object]]) -> "SupersessionGraph":
        """Build from the ``load_markdown_plan`` task-dict shape (``id`` / ``depends_on`` /
        ``supersedes``)."""
        task_list = list(tasks)
        known = [str(task["id"]) for task in task_list]
        dependencies = {
            str(task["id"]): [str(dep) for dep in (task.get("depends_on") or ())]
            for task in task_list
        }
        edges = [
            Supersession(replacement=str(task["id"]), superseded=str(superseded))
            for task in task_list
            for superseded in (task.get("supersedes") or ())
        ]
        return cls(edges, known_ids=known, dependencies=dependencies)

    @staticmethod
    def _detect_cycle(edges: list[Supersession]) -> None:
        adjacency: dict[str, list[str]] = {}
        for edge in edges:
            adjacency.setdefault(edge.replacement, []).append(edge.superseded)
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {}

        def visit(node: str, trail: list[str]) -> None:
            color[node] = GRAY
            trail.append(node)
            for nxt in adjacency.get(node, ()):
                state = color.get(nxt, WHITE)
                if state == GRAY:
                    members = trail[trail.index(nxt):] + [nxt]
                    raise SupersessionError(
                        "cyclic supersession: " + " -> ".join(members),
                        "supersession-cycle",
                    )
                if state == WHITE:
                    visit(nxt, trail)
            trail.pop()
            color[node] = BLACK

        for edge in edges:
            if color.get(edge.replacement, WHITE) == WHITE:
                visit(edge.replacement, [])

    # --- queries -----------------------------------------------------------------------

    @property
    def edges(self) -> tuple[Supersession, ...]:
        return self._edges

    def replacement_for(self, superseded: str) -> str | None:
        """The task ID declared to supersede ``superseded``, or ``None``."""
        return self._by_superseded.get(superseded)

    def satisfied_by(self, dependency: str, statuses: Mapping[str, str]) -> str | None:
        """The verified task ID that satisfies ``dependency``, or ``None``.

        ``dependency`` counts when it is itself ``verified``; otherwise the chain of
        replacements is followed (each hop is unambiguous by construction) and the first
        ``verified`` replacement wins. ``statuses`` is only read — a blocked predecessor is
        never rewritten.
        """
        node: str | None = dependency
        seen: set[str] = set()
        while node is not None and node not in seen:
            if statuses.get(node) == _VERIFIED:
                return node
            seen.add(node)
            node = self._by_superseded.get(node)
        return None

    def is_satisfied(self, dependency: str, statuses: Mapping[str, str]) -> bool:
        """True when ``dependency`` is verified or a verified replacement supersedes it."""
        return self.satisfied_by(dependency, statuses) is not None

    def unmet_dependencies(
        self, depends_on: Iterable[str], statuses: Mapping[str, str]
    ) -> tuple[str, ...]:
        """The declared edges that are neither verified nor satisfied by a replacement,
        in the given order."""
        return tuple(
            dep for dep in depends_on if not self.is_satisfied(dep, statuses)
        )
