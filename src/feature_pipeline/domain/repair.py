"""The validated repair-loop bound.

``feature_pipeline.contracts.TaskSpec.build`` rejects a negative or non-integer
``max_repair_attempts`` with ``SchemaError`` — but only when a task file is parsed. The
``--max-repair-attempts`` CLI override reaches ``pipeline_core.execution._apply_repair_bound``
through ``dataclasses.replace``, which skips that check, so a negative bound is accepted into
the run and only rejected later, when the repair loop starts (finding F-08,
``tests/test_critical_behavior_characterization.py``).

:class:`RepairBound` is the boundary value that closes the gap: the same rejection, the same
message, raised as a :class:`~feature_pipeline.domain.errors.DomainError` before any side
effect (``docs/adr/007`` §5).

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidControl

#: The default declared by ``feature_pipeline.contracts.TaskSpec.build``.
DEFAULT_MAX_REPAIR_ATTEMPTS = 2

#: The message ``contracts`` raises; preserved so a migrated call site reads identically.
_MESSAGE = "max_repair_attempts must be a non-negative integer"


@dataclass(frozen=True)
class RepairBound:
    """A non-negative repair-loop bound, validated on construction."""

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise InvalidControl(_MESSAGE)
        if self.value < 0:
            raise InvalidControl(_MESSAGE)

    @classmethod
    def default(cls) -> "RepairBound":
        """The bound a task keeps when no override is supplied."""
        return cls(DEFAULT_MAX_REPAIR_ATTEMPTS)

    def __int__(self) -> int:
        return self.value


__all__ = ["DEFAULT_MAX_REPAIR_ATTEMPTS", "RepairBound"]
