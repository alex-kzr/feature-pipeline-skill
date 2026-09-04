"""Read-only repository inspection at a diagnostic boundary (BL-02 finding F-05).

A diagnostic report must carry ``git diff`` and ``git status`` *as they were at the moment
of failure*, or say plainly that the evidence could not be collected. The historical writer
took ``git_diff`` / ``git_status`` string parameters that defaulted to empty and the
production call site passed neither, so an uncollected section rendered as an empty
*successful* one (ADR 007 §-diagnostics).

This port is the boundary :class:`feature_pipeline.application.diagnostic_service.DiagnosticService`
queries. An implementation returns a :class:`RepositorySnapshot`; it never raises for an
ordinary "git is unavailable here" condition — it records that as an explicit per-field
error instead.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RepositorySnapshot:
    """``git diff`` / ``git status`` text collected at one diagnostic boundary.

    ``diff`` / ``status`` hold the collected text (possibly empty for a genuinely clean
    tree). ``diff_error`` / ``status_error`` are set — to a non-empty, human-readable
    reason — only when that field could **not** be collected; the corresponding text field
    is then empty. :attr:`complete` is the single predicate the service uses to decide
    whether the diagnostic carries full evidence or an explicit collection failure.
    """

    diff: str
    status: str
    diff_error: str | None = None
    status_error: str | None = None

    def __post_init__(self) -> None:
        if self.diff_error is not None and not self.diff_error.strip():
            raise ValueError("diff_error must be a non-empty reason when set")
        if self.status_error is not None and not self.status_error.strip():
            raise ValueError("status_error must be a non-empty reason when set")

    @property
    def complete(self) -> bool:
        """Both fields were collected (each may still be legitimately empty)."""
        return self.diff_error is None and self.status_error is None

    @property
    def collection_error(self) -> str | None:
        """One combined reason string when either field failed, else ``None``."""
        reasons = [r for r in (self.diff_error, self.status_error) if r]
        return "; ".join(reasons) if reasons else None


class RepositoryInspectionPort(Protocol):
    """Collect a :class:`RepositorySnapshot` for ``repo_root``. Must not raise for a missing
    or unavailable Git — return the snapshot with the per-field error set instead."""

    def snapshot(self, repo_root: str | Path) -> RepositorySnapshot: ...
