"""Provenance-bearing resolved controls.

An operational control (a CLI flag, a profile setting, a task field) reaches a run either as
an explicit caller value or as a documented default. ``pipeline_core`` already records that
distinction as the string ``"explicit"`` / ``"default"`` on ``run.json``
(``pipeline_core.state.Run.set_control``, ``pipeline_core.execution._controls_map``). DF-02
lifts it into one typed value so a control is *resolved once*, at the boundary, and carries
its source with it for later plan persistence (AC-2).

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

_T = TypeVar("_T")


class ControlSource(StrEnum):
    """Where a resolved control's value came from.

    The member *values* are byte-identical to the ``sourced`` labels ``pipeline_core``
    persists, so a migrated call site writes the same ``run.json``.
    """

    EXPLICIT = "explicit"
    DEFAULT = "default"


@dataclass(frozen=True)
class ResolvedControl(Generic[_T]):
    """A control's resolved value plus the source it was resolved from."""

    name: str
    value: _T
    source: ControlSource

    @property
    def explicit(self) -> bool:
        """True when the caller supplied the value."""
        return self.source is ControlSource.EXPLICIT

    @property
    def is_default(self) -> bool:
        """True when the value is the documented default."""
        return self.source is ControlSource.DEFAULT

    def as_record(self) -> dict[str, object]:
        """The ``{"value", "sourced"}`` mapping ``Run.set_control`` persists."""
        return {"value": self.value, "sourced": str(self.source)}

    @classmethod
    def resolve(
        cls, name: str, explicit_value: _T | None, *, default: _T
    ) -> "ResolvedControl[_T]":
        """Resolve ``name`` to ``explicit_value`` if given, else ``default``.

        Provenance follows the same rule ``_controls_map`` uses: a non-``None`` caller value
        is ``EXPLICIT``; anything else is the ``DEFAULT``.
        """
        if explicit_value is None:
            return cls(name, default, ControlSource.DEFAULT)
        return cls(name, explicit_value, ControlSource.EXPLICIT)


__all__ = ["ControlSource", "ResolvedControl"]
