"""Deterministic schema-v2 -> schema-v3 run-state migration and the read entry point.

**DS-02** (``docs/plans/tasks/DS-02_state-schema-v3-migration.md``).

:func:`migrate_v2_to_v3` is a pure in-memory transform: it never touches the source file,
never mutates its argument, and returns the same value for the same input every time
(``docs/adr/007`` §3 — "Loading never silently rewrites a file"). The durable write happens
only later, through an explicit :class:`~feature_pipeline.infrastructure.state.repository.StateRepository`
commit.

The v2 -> v3 delta is deliberately small: v3 is v2's field set validated exhaustively, plus
two explicit version fields kept apart (``schema_version`` for this file's shape,
``contract_version`` for the unrelated task-spec contract) and a reserved ``revision`` token
(``docs/adr/005``; the compare-and-set check itself is DS-03's). Every recorded v2 fact —
task status, attempts, blockers, dependencies, verification verdicts, execution evidence,
history, commands, stages, artifacts — is carried across unchanged.

Standard library only.
"""

from __future__ import annotations

import json
from typing import Mapping

from .errors import SchemaVersionError
from .schema_v3 import (
    CONTRACT_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    RunStateV3,
    UnknownFieldPolicy,
)


def _plain_copy(payload: Mapping[str, object]) -> dict[str, object]:
    """A deep copy with only JSON scalar/container types and the argument left untouched."""
    return json.loads(json.dumps(payload))


def migrate_v2_to_v3(
    payload: Mapping[str, object],
    *,
    unknown_fields: UnknownFieldPolicy = "reject",
) -> dict[str, object]:
    """Return ``payload`` (a schema-v2 run state) converted to schema v3.

    Pure and deterministic. Raises
    :class:`~feature_pipeline.infrastructure.state.errors.SchemaVersionError` if ``payload``
    is not schema v2, and any other
    :class:`~feature_pipeline.infrastructure.state.errors.StateSchemaError` (with a field
    path) if a converted field does not satisfy the strict v3 schema — the migration fails
    closed rather than emitting a v3 document it could not itself read back.
    """
    version = payload.get("schema_version")
    if version != 2:
        raise SchemaVersionError(
            f"migrate_v2_to_v3 expects schema_version 2, got {version!r}",
            field="schema_version",
        )
    migrated = _plain_copy(payload)
    migrated["schema_version"] = STATE_SCHEMA_VERSION
    migrated.setdefault("contract_version", CONTRACT_SCHEMA_VERSION)
    migrated.setdefault("revision", 0)
    # Fail closed: the migration output must satisfy the strict v3 reader.
    RunStateV3.from_mapping(migrated, unknown_fields=unknown_fields)
    return migrated


def load_state(
    payload: Mapping[str, object],
    *,
    unknown_fields: UnknownFieldPolicy = "reject",
) -> RunStateV3:
    """Parse an already-decoded run-state mapping into a :class:`RunStateV3`.

    A schema-v2 payload is migrated in memory first; a schema-v3 payload is read directly.
    Any other ``schema_version`` — missing, non-integer, v1 (whose v1->v2 step lives in
    ``pipeline_core.state`` and is out of this boundary), or a future number — fails closed
    with a ``schema_version`` field diagnostic. Nothing here writes.
    """
    version = payload.get("schema_version")
    if version is None:
        raise SchemaVersionError("schema_version is missing", field="schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SchemaVersionError(
            f"schema_version must be an integer, got {type(version).__name__}",
            field="schema_version",
        )
    if version == STATE_SCHEMA_VERSION:
        return RunStateV3.from_mapping(payload, unknown_fields=unknown_fields)
    if version == 2:
        return RunStateV3.from_mapping(
            migrate_v2_to_v3(payload, unknown_fields=unknown_fields),
            unknown_fields=unknown_fields,
        )
    raise SchemaVersionError(
        f"schema_version {version} is not supported (this reader handles 2 and 3)",
        field="schema_version",
    )


__all__ = ["migrate_v2_to_v3", "load_state"]
