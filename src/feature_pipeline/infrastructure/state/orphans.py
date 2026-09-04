"""Orphan-evidence reconciliation for a run directory (``docs/adr/004`` / ``docs/adr/005``).

**DS-03** (``docs/plans/tasks/DS-03_transactional-state-repository.md``).

If the process dies after an evidence file is published but before the state snapshot that
references it is committed, the file is an *orphan*: it exists under ``reports/`` or
``logs/`` but no ``run.json`` reference names it. On the next load the run reconciles —
every orphan is **reported and moved to** ``<run-id>/orphans/`` (subpath preserved). It is
never deleted and never silently adopted into ``run.json``, because the storage tree may
hold adjacent user data.

Standard library only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from feature_pipeline.ports.artifacts import OrphanReport

from .durability import fsync_dir
from .run_layout import RunLayout

#: Directories scanned for orphans. ``orphans/`` itself and the top-level ``run.json`` /
#: ``latest.json`` are never candidates.
_SCANNED = ("reports", "logs")


def _run_relative(layout: RunLayout, path: Path) -> str:
    return path.relative_to(layout.run_dir).as_posix()


def reconcile_orphans(
    layout: RunLayout, referenced: Iterable[str]
) -> tuple[OrphanReport, ...]:
    """Quarantine every ``reports/`` or ``logs/`` file not named in ``referenced``.

    ``referenced`` holds run-relative POSIX paths (as stored in ``run.json``). Returns one
    :class:`~feature_pipeline.ports.artifacts.OrphanReport` per moved file, sorted by path.
    """
    keep = {ref.replace("\\", "/") for ref in referenced}
    moved: list[OrphanReport] = []
    for name in _SCANNED:
        base = layout.run_dir / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = _run_relative(layout, path)
            if rel in keep:
                continue
            destination = layout.orphans_dir / path.relative_to(layout.run_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            path.replace(destination)
            moved.append(
                OrphanReport(original=rel, moved_to=_run_relative(layout, destination))
            )
    if moved:
        fsync_dir(layout.orphans_dir)
    return tuple(moved)


__all__ = ["reconcile_orphans"]
