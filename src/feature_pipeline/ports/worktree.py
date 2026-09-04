"""The worktree-attribution port.

Before WT-01 the runner attributed one executor launch by taking a full content snapshot
of every VCS-relevant path across every repository boundary, immediately before and after
the launch, and subtracting the two. Snapshot I/O and peak memory were therefore
``O(total repository bytes)`` per window (BL-02 finding **F-16**), regardless of how few
files the executor actually touched.

WT-01 replaces that with *bounded, candidate-based* attribution. Only three sets of paths
can carry a change the runner must attribute:

* paths already dirty when the window opened,
* paths untracked when the window opened,
* paths that Git reports as changed when the window closes.

Their union is the *candidate set*. The inspector reads Git and index metadata for the
whole repository (cheap, no file bodies) and file bytes only for candidates, so its cost is
``O(changed candidates)`` — never the repository total.

This module is the typed vocabulary the mechanism produces and callers depend on:

* :class:`RepositoryBoundary` — one Git work tree in scope (the working root, a nested
  repository, or a declared submodule);
* :class:`CandidateChange` — one attributed path with its change kind, symlink flag, owning
  boundary, content digest, and allowed-scope classification;
* :class:`WorktreeInspection` — the whole result: availability, a confidence grade, the
  boundary set, the candidate set, and a stated reason when attribution is not trustworthy.

A :class:`WorktreeInspection` is never collapsed to an empty *successful* result: a missing
repository boundary, a failed Git query, or a candidate whose bytes could not be read is
:data:`CONFIDENCE_UNAVAILABLE` with a reason (WT-01 AC-2).

The concrete implementation is
:class:`feature_pipeline.infrastructure.worktree.inspector.GitWorktreeInspector`.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Sequence, runtime_checkable

# --- classification labels (byte-identical to ``pipeline_core.worktree``) -------------------

IN_SCOPE = "in_allowed_scope"
OUT_OF_SCOPE = "out_of_scope"

# --- confidence grades --------------------------------------------------------------------

#: The candidate set was computed from a trustworthy before/after comparison and at least
#: one path changed.
CONFIDENCE_KNOWN = "known"
#: The comparison was trustworthy and nothing is attributable to the window.
CONFIDENCE_KNOWN_EMPTY = "known-empty"
#: Attribution could not be trusted — a missing boundary, a failed Git query, or an
#: unreadable candidate. Never presented as an empty successful manifest.
CONFIDENCE_UNAVAILABLE = "unavailable"

_CONFIDENCE_VALUES = frozenset(
    {CONFIDENCE_KNOWN, CONFIDENCE_KNOWN_EMPTY, CONFIDENCE_UNAVAILABLE}
)


class BoundaryKind(StrEnum):
    """How one repository boundary entered scope."""

    WORKING_ROOT = "working-root"
    NESTED_REPOSITORY = "nested-repository"
    SUBMODULE = "submodule"


class ChangeKind(StrEnum):
    """The change one candidate path carries across the window.

    A rename is reported as a :attr:`DELETED` of the old path plus an :attr:`ADDED` of the
    new one — the same shape the full-snapshot inspector produced, so parity fixtures do not
    have to special-case it.
    """

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


class WorktreeInspectionError(RuntimeError):
    """A worktree-attribution failure the caller must not paper over.

    ``code`` is a stable, machine-readable reason; it is one of ``invalid-allowed-scope``,
    ``implementation-evidence-overwrite``, or ``marker-mismatch``.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RepositoryBoundary:
    """One Git work tree the inspector must cross when attributing a window.

    ``path`` is repository-root-relative POSIX (``""`` for the working root itself).
    ``dirty_candidate_count`` / ``untracked_candidate_count`` are the pre-window counts the
    inspector actually read bytes for, retained so a caller can show the bounded read cost.
    """

    path: str
    kind: BoundaryKind
    dirty_candidate_count: int = 0
    untracked_candidate_count: int = 0


@dataclass(frozen=True)
class CandidateChange:
    """One path attributed to the executor window.

    ``digest`` is ``"sha256:<hex>"`` of the *after* content, or ``None`` for a deletion.
    ``is_symlink`` is true when the after path is a symbolic link (its link target, not its
    referent's bytes, is what the digest and diff describe). ``boundary`` is the ``path`` of
    the owning :class:`RepositoryBoundary`.
    """

    path: str
    change: ChangeKind
    classification: str  # IN_SCOPE | OUT_OF_SCOPE
    digest: str | None
    is_symlink: bool = False
    boundary: str = ""


@dataclass(frozen=True)
class WorktreeInspection:
    """The bounded attribution of one executor launch window.

    ``available`` is false only alongside :data:`CONFIDENCE_UNAVAILABLE`; ``reason`` is then
    non-empty. ``candidates`` is ordered by ``path``.
    """

    available: bool
    confidence: str
    boundaries: tuple[RepositoryBoundary, ...]
    candidates: tuple[CandidateChange, ...]
    diff_text: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.confidence not in _CONFIDENCE_VALUES:
            raise WorktreeInspectionError(
                f"unknown attribution confidence {self.confidence!r}", "marker-mismatch"
            )

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(candidate.path for candidate in self.candidates)


@runtime_checkable
class WorktreeInspector(Protocol):
    """Attributes one executor launch window at ``O(changed candidates)`` cost."""

    def capture_marker(
        self, repo_root: str, *, exclude_roots: Sequence[str] = ()
    ) -> "WorktreeMarker":
        """Record the cheap before-state: the boundary set, per-boundary index metadata,
        and file bytes for the paths already dirty or untracked. No clean file body is
        read."""
        ...

    def attribute(
        self,
        marker: "WorktreeMarker",
        repo_root: str,
        *,
        allowed_scope: Sequence[str],
        exclude_roots: Sequence[str] = (),
    ) -> WorktreeInspection:
        """Close the window opened by ``marker`` and return the bounded attribution."""
        ...


@runtime_checkable
class WorktreeMarker(Protocol):
    """An opaque, inspector-owned record of the state a window opened in."""

    @property
    def repo_root(self) -> str: ...

    @property
    def available(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...


__all__ = [
    "IN_SCOPE",
    "OUT_OF_SCOPE",
    "CONFIDENCE_KNOWN",
    "CONFIDENCE_KNOWN_EMPTY",
    "CONFIDENCE_UNAVAILABLE",
    "BoundaryKind",
    "ChangeKind",
    "WorktreeInspectionError",
    "RepositoryBoundary",
    "CandidateChange",
    "WorktreeInspection",
    "WorktreeInspector",
    "WorktreeMarker",
]
