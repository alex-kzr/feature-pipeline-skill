"""Transactional run-evidence storage.

**DS-03** (``docs/plans/tasks/DS-03_transactional-state-repository.md``, Phase 5 of
``docs/plans/2026-09-03-feature-pipeline-refactor.md``).

:class:`~feature_pipeline.infrastructure.artifacts.store.FileArtifactStore` implements
:class:`feature_pipeline.ports.artifacts.ArtifactStorePort` over one run directory: every
report and log is published through the ADR-005 crash-safe write primitive, and orphaned
evidence (published but never referenced, because a crash landed between the two steps) is
reconciled into ``orphans/`` on the next load.

Standard library only.
"""

from __future__ import annotations

from .store import FileArtifactStore

__all__ = ["FileArtifactStore"]
