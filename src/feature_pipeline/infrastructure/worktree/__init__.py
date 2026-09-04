"""Bounded, candidate-based worktree attribution (WT-01).

:class:`~feature_pipeline.infrastructure.worktree.inspector.GitWorktreeInspector` is the
concrete :class:`feature_pipeline.ports.worktree.WorktreeInspector`. It attributes one
executor launch window by reading Git and index metadata for the whole repository and file
bytes only for the paths that could carry a change — the union of the paths dirty or
untracked when the window opened and the paths Git reports changed when it closed.

Standard library only.
"""

from __future__ import annotations

from .inspector import GitWorktreeInspector, WorktreeMarkerRecord

__all__ = ["GitWorktreeInspector", "WorktreeMarkerRecord"]
