"""The explicit-commit boundary for durable schema-v3 run state.

**DS-02** (``docs/plans/tasks/DS-02_state-schema-v3-migration.md``).

Two operations, and only two:

* :meth:`StateRepository.load` reads a ``run.json`` from disk and returns a
  :class:`~feature_pipeline.infrastructure.state.schema_v3.RunStateV3`. It **never writes** —
  a v2 file is migrated in memory only (``docs/adr/007`` §3).
* :meth:`StateRepository.commit` is the **only** writer. It bumps the ``revision`` token,
  serializes deterministic schema v3, and publishes it with an atomic
  temp-then-:func:`os.replace`.

The full ADR-005 durability sequence — ``fsync`` of the file and its directory, and the
compare-and-set ``state-revision-conflict`` rejection of a stale writer — is **DS-03's** to
add on top of this boundary. DS-02 fixes the schema shape, the field-level diagnostics, and
the read-only-load / explicit-commit split; it reserves the ``revision`` wire field and
increments it per commit so DS-03 has the token it needs.

Standard library only.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from .migration import load_state
from .schema_v3 import RunStateV3, UnknownFieldPolicy


class StateRepository:
    """Read-only load and single-writer commit for schema-v3 ``run.json`` files."""

    def load(
        self,
        path: str | os.PathLike[str],
        *,
        unknown_fields: UnknownFieldPolicy = "reject",
    ) -> RunStateV3:
        """Read and parse ``path``. Never writes; a v2 file is migrated in memory only."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return load_state(raw, unknown_fields=unknown_fields)

    def commit(
        self, state: RunStateV3, path: str | os.PathLike[str]
    ) -> RunStateV3:
        """Write ``state`` to ``path`` as deterministic schema v3, revision bumped by one.

        Returns the committed state (with the new ``revision``). The write is atomic via a
        unique temporary neighbour and :func:`os.replace`; ``fsync`` and the revision
        compare-and-set land in DS-03.
        """
        committed = state.with_revision(state.revision + 1)
        text = json.dumps(committed.to_mapping(), indent=2, ensure_ascii=False) + "\n"
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return committed


__all__ = ["StateRepository"]
