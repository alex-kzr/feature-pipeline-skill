"""The filesystem :class:`~feature_pipeline.ports.artifacts.ArtifactStorePort` for a run.

**DS-03** (``docs/plans/tasks/DS-03_transactional-state-repository.md``).

Every evidence file for one run is written here, into
``<storage>/<feature>/<run-id>/{reports,logs}/`` (``docs/adr/004`` layout), through the
ADR-005 crash-safe primitive :func:`feature_pipeline.infrastructure.state.durability.atomic_write`
— a unique same-directory temp file, ``flush`` + ``fsync``, ``os.replace``, directory
``fsync``. The store performs **no redaction**: it is pure mechanism, and canonical evidence
keeps the validated bytes the caller supplied (``docs/adr/005`` — redaction is a
presentation-boundary concern).

``publish`` returns only once the bytes are durable, so a caller may safely reference the
returned :class:`~feature_pipeline.ports.artifacts.ArtifactRef` from a state snapshot it is
about to commit — the publish-before-reference ordering AC-1 requires.

Standard library only.
"""

from __future__ import annotations

from typing import Iterable

from feature_pipeline.infrastructure.state.durability import atomic_write
from feature_pipeline.infrastructure.state.orphans import reconcile_orphans
from feature_pipeline.infrastructure.state.run_layout import RunLayout
from feature_pipeline.ports.artifacts import (
    ArtifactKind,
    ArtifactNameError,
    ArtifactRef,
    OrphanReport,
)

_FORBIDDEN = ("/", "\\", "\x00")


def _validate_name(name: str) -> str:
    """Reject an empty, absolute, traversing, or separator-bearing artifact name."""
    if not name or name in (".", ".."):
        raise ArtifactNameError(f"artifact name {name!r} is empty or a directory reference")
    if any(token in name for token in _FORBIDDEN) or name.startswith("."):
        raise ArtifactNameError(
            f"artifact name {name!r} must be a bare filename inside the run directory"
        )
    return name


class FileArtifactStore:
    """A per-run evidence store rooted at one :class:`RunLayout`."""

    def __init__(self, layout: RunLayout, *, fsync: bool = True) -> None:
        self._layout = layout
        self._fsync = fsync

    def publish(
        self, kind: ArtifactKind, name: str, content: str | bytes
    ) -> ArtifactRef:
        """Write ``content`` under ``reports/`` or ``logs/`` crash-safely; return its ref."""
        _validate_name(name)
        directory = self._layout.dir_for(kind)
        directory.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        atomic_write(directory / name, data, fsync=self._fsync)
        rel = (directory / name).relative_to(self._layout.run_dir).as_posix()
        return ArtifactRef(kind=kind, relative_path=rel)

    def reconcile_orphans(self, referenced: Iterable[str]) -> tuple[OrphanReport, ...]:
        """Quarantine every ``reports/`` / ``logs/`` file the state does not reference."""
        return reconcile_orphans(self._layout, referenced)


__all__ = ["FileArtifactStore"]
