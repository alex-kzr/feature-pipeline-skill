"""Strict, versioned schema-v3 persistence boundary for durable run state.

**DS-02** (``docs/plans/tasks/DS-02_state-schema-v3-migration.md``, Phase 5 of
``docs/plans/2026-09-03-feature-pipeline-refactor.md``).

This package is the one place a ``run.json`` is turned into typed state and back:

* :mod:`.schema_v3` — exhaustive DTO readers/writers. Every field is read explicitly; the
  first violation raises a :class:`StateSchemaError` naming the exact ``field`` path. An
  explicit unknown-field policy (``reject`` / ``ignore`` / ``preserve``) replaces silent
  tolerance.
* :mod:`.migration` — :func:`migrate_v2_to_v3` (a pure in-memory transform that never
  rewrites the source) and :func:`load_state` (detect the version, migrate v2, parse v3).
* :mod:`.repository` — :class:`StateRepository`: read-only :meth:`~StateRepository.load`
  and single-writer :meth:`~StateRepository.commit` (deterministic v3, ``revision`` bumped).

``schema_version`` (this file's shape, ``3``) is kept distinct from ``contract_version``
(``feature_pipeline.contracts.SCHEMA_VERSION`` — the unrelated task-spec/role/profile
contract), per ``docs/adr/007`` §3.

The full ADR-005 durability sequence (``fsync``, directory ``fsync``, revision
compare-and-set) is layered on by **DS-03**; DS-02 owns the schema, the diagnostics, and
the read/commit split.

Standard library only.
"""

from __future__ import annotations

from .errors import (
    FieldTypeError,
    FieldValueError,
    MissingFieldError,
    SchemaVersionError,
    StateSchemaError,
    UnknownFieldError,
)
from .migration import load_state, migrate_v2_to_v3
from .repository import StateRepository
from .schema_v3 import (
    CONTRACT_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    SUPPORTED_READ_SCHEMA_VERSIONS,
    CommandRecordV3,
    EvidenceV3,
    HistoryEventV3,
    ImplementationV3,
    RunStateV3,
    TaskEntryV3,
    UnknownFieldPolicy,
    VerificationV3,
)

__all__ = [
    # versions
    "STATE_SCHEMA_VERSION",
    "SUPPORTED_READ_SCHEMA_VERSIONS",
    "CONTRACT_SCHEMA_VERSION",
    # errors
    "StateSchemaError",
    "SchemaVersionError",
    "MissingFieldError",
    "FieldTypeError",
    "FieldValueError",
    "UnknownFieldError",
    # DTOs
    "RunStateV3",
    "TaskEntryV3",
    "VerificationV3",
    "ImplementationV3",
    "EvidenceV3",
    "HistoryEventV3",
    "CommandRecordV3",
    "UnknownFieldPolicy",
    # entry points
    "migrate_v2_to_v3",
    "load_state",
    "StateRepository",
]
