"""Effectful-boundary ports for the portable feature-pipeline core.

A *port* is a narrow protocol the domain and application layers depend on instead of calling
an ambient effect directly. DF-02 introduces the first two:

* :class:`~feature_pipeline.ports.clock.Clock` — the single source of "now", so a migrated
  slice takes injected time and its tests are deterministic (``docs/adr/007`` §4);
* :class:`~feature_pipeline.ports.clock.RunIdFactory` — run-identifier minting, kept
  byte-compatible with ``pipeline_core.state.Run.create``.

The duplicate ``_now`` / ``_utcnow`` helpers scattered across ``pipeline_core`` are removed as
their callers migrate onto these ports in later phases.

Standard library only.
"""

from __future__ import annotations

from .artifacts import (
    ArtifactKind,
    ArtifactNameError,
    ArtifactRef,
    ArtifactStorePort,
    OrphanReport,
)
from .clock import (
    TIMESTAMP_FORMAT,
    Clock,
    ClockRunIdFactory,
    ManualClock,
    RunIdFactory,
    SystemClock,
    format_timestamp,
)
from .state_repository import LoadedState, StateRepositoryPort, StateRevisionConflict
from .worktree import (
    CONFIDENCE_KNOWN,
    CONFIDENCE_KNOWN_EMPTY,
    CONFIDENCE_UNAVAILABLE,
    BoundaryKind,
    CandidateChange,
    ChangeKind,
    RepositoryBoundary,
    WorktreeInspection,
    WorktreeInspectionError,
    WorktreeInspector,
    WorktreeMarker,
)

__all__ = [
    "TIMESTAMP_FORMAT",
    "Clock",
    "ClockRunIdFactory",
    "ManualClock",
    "RunIdFactory",
    "SystemClock",
    "format_timestamp",
    # DS-03 — durable-persistence ports
    "ArtifactKind",
    "ArtifactNameError",
    "ArtifactRef",
    "ArtifactStorePort",
    "OrphanReport",
    "LoadedState",
    "StateRepositoryPort",
    "StateRevisionConflict",
    # WT-01 — bounded worktree-attribution port
    "CONFIDENCE_KNOWN",
    "CONFIDENCE_KNOWN_EMPTY",
    "CONFIDENCE_UNAVAILABLE",
    "BoundaryKind",
    "CandidateChange",
    "ChangeKind",
    "RepositoryBoundary",
    "WorktreeInspection",
    "WorktreeInspectionError",
    "WorktreeInspector",
    "WorktreeMarker",
]
