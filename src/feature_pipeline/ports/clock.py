"""The clock and run-ID ports.

``pipeline_core`` carries five identical private helpers —
``pipeline_core.state._now``, ``pipeline_core.execution._utcnow``,
``pipeline_core.lease._now``, ``pipeline_core.concurrency._now``,
``pipeline_core.diagnostics._now`` — each formatting ``datetime.now(timezone.utc)`` as
``"%Y-%m-%dT%H:%M:%SZ"``. DF-02 replaces that duplication with one :class:`Clock` port plus
:func:`format_timestamp`, and one :class:`RunIdFactory` port for the ``run_id`` composition in
``pipeline_core.state.Run.create``.

Nothing here reads the wall clock except :class:`SystemClock`. A migrated call site takes a
``Clock`` argument, so its tests pass a :class:`ManualClock` and assert exact timestamps
(``docs/plans/tasks/DF-02_resolved-controls-clocks.md`` AC-3).

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

#: The one wire format every ``pipeline_core`` timestamp helper emits.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def format_timestamp(moment: datetime) -> str:
    """Render ``moment`` exactly as the ``_now`` / ``_utcnow`` helpers do.

    The instant must be timezone-aware; it is normalised to UTC before formatting so a
    non-UTC clock cannot shift a persisted timestamp.
    """
    if moment.tzinfo is None:
        raise ValueError("clock timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


@runtime_checkable
class Clock(Protocol):
    """The single source of "now" for a migrated boundary."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware :class:`datetime`."""
        ...


class SystemClock:
    """The production clock: the real UTC wall time."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class ManualClock:
    """A deterministic :class:`Clock` for tests.

    Time is fixed at ``moment`` and only ever moves when :meth:`advance` is called.
    """

    moment: datetime = field(
        default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    def __post_init__(self) -> None:
        if self.moment.tzinfo is None:
            raise ValueError("ManualClock needs a timezone-aware start instant")

    def now(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self.moment = self.moment + timedelta(seconds=seconds)


@runtime_checkable
class RunIdFactory(Protocol):
    """Mints a run identifier for a feature slug."""

    def new_run_id(self, feature: str) -> str:
        ...


@dataclass(frozen=True)
class ClockRunIdFactory:
    """A :class:`RunIdFactory` that stamps the identifier from an injected :class:`Clock`.

    The composition — ``"<timestamp with ':' -> '-'>-<feature>"`` — is byte-identical to
    ``pipeline_core.state.Run.create``.
    """

    clock: Clock

    def new_run_id(self, feature: str) -> str:
        stamp = format_timestamp(self.clock.now())
        return f"{stamp.replace(':', '-')}-{feature}"


__all__ = [
    "TIMESTAMP_FORMAT",
    "Clock",
    "ClockRunIdFactory",
    "ManualClock",
    "RunIdFactory",
    "SystemClock",
    "format_timestamp",
]
