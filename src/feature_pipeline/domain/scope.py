"""Allowed-scope classification for the deterministic scope gate.

**WT-02** (``docs/plans/tasks/WT-02_deterministic-scope-gate.md``, ``docs/adr/007`` §6).

A task's ``Allowed scope`` is a list of repository-relative path globs. Every path the
executor window changed is either *in* that scope or *out of* it; an out-of-scope,
executor-owned change is a mechanical contract violation the runner must act on structurally
rather than delegate to an LLM verifier.

:class:`AllowedScope` is the domain value object that answers "is this path in scope?" using
the one canonical matcher, :meth:`feature_pipeline.domain.paths.RelativeGlob.matches`. The
label strings (:data:`IN_ALLOWED_SCOPE` / :data:`OUT_OF_SCOPE`) are byte-identical to the
ones ``pipeline_core.worktree`` and :mod:`feature_pipeline.ports.worktree` already emit, so a
persisted manifest is unchanged by the cutover.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .paths import RelativeGlob, RelativePath

#: A changed path that lies within the task's declared ``Allowed scope``.
IN_ALLOWED_SCOPE = "in_allowed_scope"
#: A changed path that lies outside it.
OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True)
class AllowedScope:
    """The parsed ``Allowed scope`` of one task — an ordered tuple of path globs.

    Construction fails closed: an absolute, drive-qualified, ``~``-rooted, or
    anchor-traversing entry raises
    :class:`~feature_pipeline.domain.errors.InvalidGlob` (``code`` ``"invalid-glob"``)
    before any classification happens.
    """

    patterns: tuple[RelativeGlob, ...]

    @classmethod
    def parse(cls, entries: Iterable[object]) -> "AllowedScope":
        return cls(tuple(RelativeGlob.parse(entry) for entry in entries))

    def contains(self, path: "str | RelativePath") -> bool:
        """True when ``path`` matches at least one scope glob."""
        return any(pattern.matches(path) for pattern in self.patterns)

    def classify(self, path: "str | RelativePath") -> str:
        """:data:`IN_ALLOWED_SCOPE` when :meth:`contains`, else :data:`OUT_OF_SCOPE`."""
        return IN_ALLOWED_SCOPE if self.contains(path) else OUT_OF_SCOPE


@dataclass(frozen=True)
class ScopeViolation:
    """One executor-owned change the gate found outside the allowed scope.

    ``change`` is the :class:`feature_pipeline.ports.worktree.ChangeKind` value
    (``"added"`` / ``"modified"`` / ``"deleted"``); ``boundary`` is the repository-relative
    path of the owning Git work tree (``""`` for the working root); ``digest`` is the
    ``"sha256:<hex>"`` of the after-content, or ``None`` for a deletion.
    """

    path: str
    change: str
    boundary: str = ""
    digest: str | None = None


__all__ = [
    "IN_ALLOWED_SCOPE",
    "OUT_OF_SCOPE",
    "AllowedScope",
    "ScopeViolation",
]
