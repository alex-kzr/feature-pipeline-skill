"""The single diagnostic boundary: collect repository evidence, then render the report.

Every failure path in the task engine and the independent-verification orchestration writes
its pre-transition diagnostic through :class:`DiagnosticService`. The service queries a
read-only :class:`~feature_pipeline.ports.diagnostics.RepositoryInspectionPort` for
``git diff`` / ``git status`` *at the moment of failure* and hands the renderer either the
collected text or an explicit ``diagnostic-collection-failed`` marker — a section that could
not be collected is never presented as an empty successful one (BL-02 finding F-05,
ADR 007 §-diagnostics).

The renderer itself is unchanged: :func:`pipeline_core.diagnostics.write_diagnostic_report`
still assembles and atomically persists the ``diagnostic-report.md``. This module owns only
the evidence collection that used to be the caller's forgotten responsibility.

Standard library only.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from feature_pipeline.infrastructure.git.inspector import inspect as _git_inspect
from feature_pipeline.ports.diagnostics import (
    RepositoryInspectionPort,
    RepositorySnapshot,
)

if TYPE_CHECKING:
    from pipeline_core.state import Run

#: Prefixes the diagnostic ``Note`` when repository evidence could not be collected. A reader
#: (or a test verifier) sees the gap stated, never a silently empty ``git diff`` section.
DIAGNOSTIC_COLLECTION_FAILED = "diagnostic-collection-failed"

#: Rendered into a git section whose evidence could not be collected.
_UNAVAILABLE = "(evidence unavailable: {reason})"

#: Rendered into a git section that was collected and is genuinely empty — distinct from the
#: renderer's bare "none" placeholder, which reads as "never collected".
_DIFF_EMPTY = "(git diff reported no changes)"
_STATUS_EMPTY = "(git status reported a clean working tree)"


class GitRepositoryInspection:
    """Default port: ``git diff`` + ``git status --porcelain`` through the RS-03 read-only
    allowlist (:func:`feature_pipeline.infrastructure.git.inspector.inspect`).

    A policy denial, a launch failure, a timeout, or a non-zero exit each become the
    matching per-field error on the returned :class:`RepositorySnapshot` — never a silent
    empty success.
    """

    def __init__(self, *, timeout: float = 120.0) -> None:
        self._timeout = timeout

    def snapshot(self, repo_root: str | Path) -> RepositorySnapshot:
        diff = _git_inspect(repo_root, ["diff", "--no-color"], timeout=self._timeout)
        status = _git_inspect(repo_root, ["status", "--porcelain"], timeout=self._timeout)
        return RepositorySnapshot(
            diff=diff.stdout if diff.available else "",
            status=status.stdout if status.available else "",
            diff_error=None if diff.available else (diff.reason or "git diff unavailable"),
            status_error=(
                None if status.available else (status.reason or "git status unavailable")
            ),
        )


class DiagnosticService:
    """Collect repository evidence for one diagnostic, then persist the report.

    Construct with no arguments for production (the real read-only Git inspection); pass an
    ``inspection`` double in a test. It parses no CLI arguments and constructs no adapter —
    the only effect it owns is a read-only ``git`` inspection (AC-1).
    """

    def __init__(self, inspection: RepositoryInspectionPort | None = None) -> None:
        self._inspection = inspection if inspection is not None else GitRepositoryInspection()

    def snapshot(self, repo_root: str | Path) -> RepositorySnapshot:
        return self._inspection.snapshot(repo_root)

    def collect(
        self,
        run: "Run",
        *,
        task_id: str,
        attempt: int,
        note: str | None = None,
        reports_dir: str | Path | None = None,
    ) -> Path:
        """Write the pre-transition diagnostic for ``task_id`` with real repository evidence.

        ``git diff`` / ``git status`` are collected now, through the read-only inspection
        port. When either cannot be collected the report still renders — the section carries
        an explicit ``evidence unavailable`` line and the ``Note`` is prefixed with
        :data:`DIAGNOSTIC_COLLECTION_FAILED` and the reason, so missing evidence is stated,
        never presented as an empty success (AC-2).
        """
        # Deferred import: the renderer lives in the legacy tree and importing it at module
        # load would couple ``src`` import time to ``pipeline_core``.
        from pipeline_core.diagnostics import write_diagnostic_report

        snap = self.snapshot(run.repo_root)
        git_diff = snap.diff if snap.diff else _DIFF_EMPTY
        git_status = snap.status if snap.status else _STATUS_EMPTY
        if snap.diff_error is not None:
            git_diff = _UNAVAILABLE.format(reason=snap.diff_error)
        if snap.status_error is not None:
            git_status = _UNAVAILABLE.format(reason=snap.status_error)

        effective_note = note
        if not snap.complete:
            marker = f"{DIAGNOSTIC_COLLECTION_FAILED}: {snap.collection_error}"
            effective_note = f"{note} | {marker}" if note else marker

        return write_diagnostic_report(
            run,
            task_id=task_id,
            attempt=attempt,
            git_diff=git_diff,
            git_status=git_status,
            note=effective_note,
            reports_dir=reports_dir,
        )
