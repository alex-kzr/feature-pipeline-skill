"""Validated identifier value objects.

``TaskId`` mirrors ``feature_pipeline.contracts.TASK_ID_RE`` (two-to-six letters, a hyphen,
one-to-four digits) so the portable core keeps accepting the same upstream board IDs.
``FeatureId`` mirrors ``pipeline_core.runner_cli._FEATURE_RE`` (a single
``[A-Za-z0-9._-]`` token) and reuses that module's exact rejection message.

Both are frozen, compare and hash by their normalized string, and ``str(id)`` returns the
canonical text unchanged.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import InvalidIdentifier

#: Board / plan task ID: ``EDF-01``, ``LT-9``, ``PCC-05``. Identical to
#: ``feature_pipeline.contracts.TASK_ID_RE``; kept looser than any one project's convention.
TASK_ID_PATTERN = re.compile(r"^[A-Za-z]{2,6}-\d{1,4}$")

#: A feature slug: one ``[A-Za-z0-9._-]`` token, no whitespace or path separators. Identical
#: to ``pipeline_core.runner_cli._FEATURE_RE``.
FEATURE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

#: The verbatim message ``runner_cli`` raises for a bad feature name (kept for parity).
_FEATURE_ID_MESSAGE = "feature name must be a single [A-Za-z0-9._-] token"


@dataclass(frozen=True)
class TaskId:
    """A validated task identifier."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not TASK_ID_PATTERN.match(self.value):
            raise InvalidIdentifier(f"invalid task ID {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class FeatureId:
    """A validated feature slug."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not FEATURE_ID_PATTERN.match(self.value):
            raise InvalidIdentifier(_FEATURE_ID_MESSAGE)

    def __str__(self) -> str:
        return self.value


__all__ = [
    "TaskId",
    "FeatureId",
    "TASK_ID_PATTERN",
    "FEATURE_ID_PATTERN",
]
