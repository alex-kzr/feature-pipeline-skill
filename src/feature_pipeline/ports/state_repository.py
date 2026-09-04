"""The durable run-state repository port.

**DS-03** (``docs/plans/tasks/DS-03_transactional-state-repository.md``, Phase 5 of
``docs/plans/2026-09-03-feature-pipeline-refactor.md``).

DS-02 gave the schema-v3 boundary a read-only ``load`` and a single-writer ``commit``.
DS-03 turns that pair into the *transactional* contract every durable mutation goes through
(``docs/adr/005``):

* :meth:`StateRepositoryPort.load_with_revision` returns the parsed state **and** the
  ``revision`` token read with it, so a caller can commit compare-and-set.
* :meth:`StateRepositoryPort.commit` is the only writer. Given ``expected_revision`` it is a
  compare-and-set: a writer whose token no longer matches what is on disk is rejected with
  :class:`StateRevisionConflict` — never last-write-wins.

The concrete file-and-``fsync`` implementation is
:class:`feature_pipeline.infrastructure.state.repository.StateRepository`; the run-directory
layout it writes into is :class:`feature_pipeline.infrastructure.state.run_layout.RunLayout`.
This module holds no filesystem code — only the protocol, the read result, and the conflict
error — so nothing here imports the infrastructure package.

Standard library only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from feature_pipeline.domain.errors import DomainError

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a ports -> infrastructure import
    from feature_pipeline.infrastructure.state.schema_v3 import (
        RunStateV3,
        UnknownFieldPolicy,
    )


class StateRevisionConflict(DomainError):
    """A commit was attempted with a ``revision`` token that no longer matches disk.

    Raised instead of overwriting a concurrent writer's work (``docs/adr/005`` — "commit is
    compare-and-set; a stale writer is rejected ... never last-write-wins"). ``expected`` is
    the token the caller held; ``found`` is what the on-disk ``run.json`` carries now.
    """

    code = "state-revision-conflict"

    def __init__(self, *, expected: int, found: int) -> None:
        super().__init__(
            f"stale state write: caller holds revision {expected}, disk is at {found}"
        )
        self.expected = expected
        self.found = found


@dataclass(frozen=True)
class LoadedState:
    """A parsed run state together with the ``revision`` token it was read at.

    Pass :attr:`revision` back as ``expected_revision`` to
    :meth:`StateRepositoryPort.commit` to make the write compare-and-set.
    """

    state: "RunStateV3"
    revision: int


@runtime_checkable
class StateRepositoryPort(Protocol):
    """Read-with-revision and compare-and-set commit for durable ``run.json`` state."""

    def load(
        self,
        path: str | os.PathLike[str],
        *,
        unknown_fields: "UnknownFieldPolicy" = "reject",
    ) -> "RunStateV3":
        """Parse ``path`` (read-only; a v2 file is migrated in memory only)."""
        ...

    def load_with_revision(
        self,
        path: str | os.PathLike[str],
        *,
        unknown_fields: "UnknownFieldPolicy" = "reject",
    ) -> LoadedState:
        """Parse ``path`` and return it paired with its on-disk ``revision`` token."""
        ...

    def commit(
        self,
        state: "RunStateV3",
        path: str | os.PathLike[str],
        *,
        expected_revision: int | None = None,
    ) -> "RunStateV3":
        """Write ``state`` as deterministic schema v3, ``revision`` bumped by one.

        With ``expected_revision`` the write is compare-and-set: if the on-disk ``revision``
        differs, :class:`StateRevisionConflict` is raised and nothing is written. The write
        itself is crash-safe — a unique same-directory temp file, ``flush`` + ``fsync``,
        ``os.replace``, then a best-effort directory ``fsync`` (``docs/adr/005``).
        """
        ...


__all__ = [
    "LoadedState",
    "StateRepositoryPort",
    "StateRevisionConflict",
]
