"""The explicit-commit boundary for durable schema-v3 run state.

**DS-02** (``docs/plans/tasks/DS-02_state-schema-v3-migration.md``) introduced the read /
commit split. **DS-03** (``docs/plans/tasks/DS-03_transactional-state-repository.md``) makes
the commit *transactional*:

* :meth:`StateRepository.load` reads a ``run.json`` and returns a
  :class:`~feature_pipeline.infrastructure.state.schema_v3.RunStateV3`. It **never writes** —
  a v2 file is migrated in memory only (``docs/adr/007`` §3).
* :meth:`StateRepository.load_with_revision` returns that state paired with the ``revision``
  token it was read at, so the caller can commit compare-and-set.
* :meth:`StateRepository.commit` is the **only** writer. It bumps ``revision``, serialises
  deterministic schema v3, and publishes it through the ADR-005 crash-safe primitive
  (:func:`feature_pipeline.infrastructure.state.durability.atomic_write` — unique temp,
  ``flush`` + ``fsync``, ``os.replace``, directory ``fsync``). Given ``expected_revision``
  it is compare-and-set: a stale writer is rejected with
  :class:`~feature_pipeline.ports.state_repository.StateRevisionConflict` and nothing is
  written (``docs/adr/005`` — "never last-write-wins").

The serialiser runs **no redaction** (``docs/adr/005`` / finding F-18): canonical state
stores the validated value resume needs; redaction is applied only when a value is rendered
into a log, report, or export.

Standard library only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from feature_pipeline.ports.state_repository import LoadedState, StateRevisionConflict

from .durability import atomic_write_text
from .migration import load_state
from .schema_v3 import RunStateV3, UnknownFieldPolicy


def _read_revision(path: Path) -> int | None:
    """The ``revision`` an existing ``run.json`` carries, or ``None`` if it is not there."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    revision = raw.get("revision") if isinstance(raw, dict) else None
    return revision if isinstance(revision, int) and not isinstance(revision, bool) else None


class StateRepository:
    """Read-only load and single-writer, compare-and-set commit for schema-v3 state."""

    def load(
        self,
        path: str | os.PathLike[str],
        *,
        unknown_fields: UnknownFieldPolicy = "reject",
    ) -> RunStateV3:
        """Read and parse ``path``. Never writes; a v2 file is migrated in memory only."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return load_state(raw, unknown_fields=unknown_fields)

    def load_with_revision(
        self,
        path: str | os.PathLike[str],
        *,
        unknown_fields: UnknownFieldPolicy = "reject",
    ) -> LoadedState:
        """Parse ``path`` and return it with the ``revision`` token it was read at."""
        state = self.load(path, unknown_fields=unknown_fields)
        return LoadedState(state=state, revision=state.revision)

    def initialise(
        self, state: RunStateV3, path: str | os.PathLike[str]
    ) -> RunStateV3:
        """Write the first snapshot of a brand-new run verbatim at ``revision`` 0.

        Refuses to touch an existing file — a fresh run never overwrites another run's
        ``run.json`` (``docs/adr/004``). Same crash-safe write as :meth:`commit`.
        """
        target = Path(path)
        if target.exists():
            raise StateRevisionConflict(expected=0, found=_read_revision(target) or 0)
        first = state.with_revision(0)
        text = json.dumps(first.to_mapping(), indent=2, ensure_ascii=False) + "\n"
        atomic_write_text(target, text)
        return first

    def commit(
        self,
        state: RunStateV3,
        path: str | os.PathLike[str],
        *,
        expected_revision: int | None = None,
    ) -> RunStateV3:
        """Write ``state`` to ``path`` as deterministic schema v3, revision bumped by one.

        Returns the committed state (with the new ``revision``). The write is crash-safe: a
        unique temporary neighbour, ``flush`` + ``fsync``, :func:`os.replace`, then a
        best-effort directory ``fsync`` (POSIX). With ``expected_revision`` set, a mismatch
        with the on-disk ``revision`` raises
        :class:`~feature_pipeline.ports.state_repository.StateRevisionConflict` and writes
        nothing.
        """
        target = Path(path)
        if expected_revision is not None:
            found = _read_revision(target)
            on_disk = expected_revision if found is None else found
            if on_disk != expected_revision:
                raise StateRevisionConflict(expected=expected_revision, found=on_disk)
        committed = state.with_revision(state.revision + 1)
        text = json.dumps(committed.to_mapping(), indent=2, ensure_ascii=False) + "\n"
        atomic_write_text(target, text)
        return committed


__all__ = ["StateRepository"]
