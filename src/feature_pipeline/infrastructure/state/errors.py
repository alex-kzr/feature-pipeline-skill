"""Field-level diagnostics for the schema-v3 run-state boundary.

Every rejection the reader makes carries two machine-readable facts: a stable ``code`` slug
(inherited from :class:`~feature_pipeline.domain.errors.DomainError`) and the ``field`` path
inside the payload that failed — ``"schema_version"``, ``"tasks[1].id"``,
``"tasks[0].execution_evidence.implementation"``. A permissive reader that defers corruption
to execution would defeat resume safety (DS-02 Risks), so nothing here coerces: an invalid
value is reported at load, pointing at exactly where it is.

Standard library only.
"""

from __future__ import annotations

from feature_pipeline.domain.errors import DomainError


class StateSchemaError(DomainError):
    """Base class for a schema-v3 payload that cannot be read as durable run state.

    ``field`` is the dotted / indexed path to the offending value inside the payload. It is
    always populated (``""`` only for a whole-document failure) and never contains a host
    path or a secret.
    """

    code = "state-schema-error"

    def __init__(self, message: str, *, field: str, code: str | None = None) -> None:
        super().__init__(message, code=code)
        self.field = field

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        return f"{self.field}: {base}" if self.field else base


class SchemaVersionError(StateSchemaError):
    """``schema_version`` is missing, not an integer, or not one this reader supports."""

    code = "unsupported-schema-version"


class MissingFieldError(StateSchemaError):
    """A required field is absent from its mapping."""

    code = "missing-field"


class FieldTypeError(StateSchemaError):
    """A field is present but its JSON type is wrong (``attempts`` a string, and so on)."""

    code = "invalid-field-type"


class FieldValueError(StateSchemaError):
    """A field has the right type but a value outside its closed domain (an unknown status)."""

    code = "invalid-field-value"


class UnknownFieldError(StateSchemaError):
    """A mapping carries a key the schema does not define and the policy is ``reject``."""

    code = "unknown-field"


__all__ = [
    "StateSchemaError",
    "SchemaVersionError",
    "MissingFieldError",
    "FieldTypeError",
    "FieldValueError",
    "UnknownFieldError",
]
