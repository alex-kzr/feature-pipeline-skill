"""The single mapping from a typed pipeline outcome to a stable CLI exit code.

Today the exit codes are spread through ``pipeline_core.runner_cli`` as the module-level
constants ``EXIT_OK`` / ``EXIT_GATE_PENDING`` / ``EXIT_BLOCKED`` / ``EXIT_ERROR`` /
``EXIT_PUSH_DENIED`` plus the ``EXIT_MEANINGS`` table, and every ``CliError`` picks one by
hand. :class:`ResultMapper` is the one place that owns that translation for the slice DF-01
migrates: a :class:`~feature_pipeline.domain.errors.DomainError` becomes an ``error`` result
(exit ``30``), and the canonical code/meaning table is exposed for the CLI to delegate to.

The numbers and the one-line meanings are the frozen baseline compatibility promise
(``tests/goldens/compatibility/exit-code-table.txt``); this module must never be the reason
one changes.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from feature_pipeline.domain.errors import DomainError


class Outcome(StrEnum):
    """A terminal pipeline outcome, independent of its numeric exit code."""

    OK = "ok"
    GATE_PENDING = "gate-pending"
    BLOCKED = "blocked"
    ERROR = "error"
    PUSH_DENIED = "push-denied"


#: Outcome -> process exit code. Identical to the ``EXIT_*`` constants in
#: ``pipeline_core.runner_cli``.
_EXIT_CODE: dict[Outcome, int] = {
    Outcome.OK: 0,
    Outcome.GATE_PENDING: 10,
    Outcome.BLOCKED: 20,
    Outcome.ERROR: 30,
    Outcome.PUSH_DENIED: 1,
}

#: Outcome -> the C5 one-line meaning. Identical to ``runner_cli.EXIT_MEANINGS`` (which is
#: keyed by code); ``PUSH_DENIED`` has no C5 line and reuses the fixed refusal wording.
_MEANING: dict[Outcome, str] = {
    Outcome.OK: "ok",
    Outcome.GATE_PENDING: "a delivery gate is pending",
    Outcome.BLOCKED: "blocked",
    Outcome.ERROR: "error",
    Outcome.PUSH_DENIED: "push is denied at this stage and the runner has no push code path.",
}


@dataclass(frozen=True)
class PipelineResult:
    """A typed outcome plus a leak-free human message."""

    outcome: Outcome
    message: str = ""

    @property
    def exit_code(self) -> int:
        return _EXIT_CODE[self.outcome]


class ResultMapper:
    """Owns every accepted outcome-to-exit-code translation for the migrated slice."""

    #: The frozen table, safe to read as ``ResultMapper.EXIT_CODES[Outcome.OK]``.
    EXIT_CODES: dict[Outcome, int] = dict(_EXIT_CODE)

    def exit_code(self, value: "PipelineResult | Outcome") -> int:
        """Return the process exit code for a result or a bare outcome."""
        outcome = value.outcome if isinstance(value, PipelineResult) else value
        return _EXIT_CODE[outcome]

    def meaning(self, value: "PipelineResult | Outcome") -> str:
        """Return the one-line meaning for a result or a bare outcome."""
        outcome = value.outcome if isinstance(value, PipelineResult) else value
        return _MEANING[outcome]

    def from_domain_error(self, error: DomainError) -> PipelineResult:
        """Map any fail-closed domain rejection to the ``error`` outcome (exit ``30``)."""
        if not isinstance(error, DomainError):  # defensive: keeps the boundary honest
            raise TypeError("from_domain_error expects a DomainError")
        return PipelineResult(Outcome.ERROR, str(error))

    def ok(self, message: str = "") -> PipelineResult:
        return PipelineResult(Outcome.OK, message)

    def gate_pending(self, message: str = "") -> PipelineResult:
        return PipelineResult(Outcome.GATE_PENDING, message)

    def blocked(self, message: str = "") -> PipelineResult:
        return PipelineResult(Outcome.BLOCKED, message)

    def push_denied(self, message: str = "") -> PipelineResult:
        return PipelineResult(
            Outcome.PUSH_DENIED, message or _MEANING[Outcome.PUSH_DENIED]
        )


__all__ = ["Outcome", "PipelineResult", "ResultMapper"]
