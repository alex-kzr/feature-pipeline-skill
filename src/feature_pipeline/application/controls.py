"""Resolve and validate ``execute``-mode controls at the CLI/application boundary.

This is the DF-02 correction for findings **F-07** (a control is accepted but never applied)
and **F-08** (a control is validated too late). :func:`resolve_execute_controls` is a pure
function: it validates every control and returns typed
:class:`~feature_pipeline.domain.controls.ResolvedControl` values *before* the caller creates
run state, acquires a lease, or launches an adapter (``docs/adr/007`` §4/§5).

The legacy path — ``pipeline_core.execution._apply_repair_bound`` via ``dataclasses.replace``
— stays in place; a later phase (CP-02 / IN-01) cuts the CLI over to this function.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

from feature_pipeline.domain.controls import ControlSource, ResolvedControl
from feature_pipeline.domain.repair import DEFAULT_MAX_REPAIR_ATTEMPTS, RepairBound


@dataclass(frozen=True)
class ResolvedExecuteControls:
    """Every ``execute``-mode control, resolved once, each carrying its source.

    DF-02 resolves the repair bound (the F-07/F-08 slice). The adapter pin and the two byte
    budgets are resolved here too as later phases migrate them; the dataclass grows a field
    per control rather than a loose dict.
    """

    max_repair_attempts: ResolvedControl[int]

    def as_records(self) -> dict[str, dict[str, object]]:
        """The ``{name: {"value", "sourced"}}`` shape ``run.json`` persists per control."""
        return {"max_repair_attempts": self.max_repair_attempts.as_record()}


def resolve_execute_controls(
    *, max_repair_attempts: int | None = None
) -> ResolvedExecuteControls:
    """Validate and resolve the ``execute``-mode controls.

    ``max_repair_attempts`` is the ``--max-repair-attempts`` override (``None`` when the flag
    was not passed). A negative or non-integer value raises
    :class:`~feature_pipeline.domain.errors.InvalidControl` here, at the boundary — never
    deferred to the repair loop.

    The resolved value for the "not passed" case is the concrete default (``2``), tagged
    ``DEFAULT``. Mapping that back to the legacy ``(None, "default")`` record shape, if a
    persistence adapter needs it, is a state-layer concern and out of DF-02's scope.
    """
    if max_repair_attempts is None:
        bound: ResolvedControl[int] = ResolvedControl(
            "max_repair_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS, ControlSource.DEFAULT
        )
    else:
        validated = RepairBound(max_repair_attempts)
        bound = ResolvedControl(
            "max_repair_attempts", int(validated), ControlSource.EXPLICIT
        )
    return ResolvedExecuteControls(max_repair_attempts=bound)


__all__ = ["ResolvedExecuteControls", "resolve_execute_controls"]
