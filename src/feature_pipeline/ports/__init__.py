"""Effectful-boundary ports for the portable feature-pipeline core.

A *port* is a narrow protocol the domain and application layers depend on instead of calling
an ambient effect directly. DF-02 introduces the first two:

* :class:`~feature_pipeline.ports.clock.Clock` — the single source of "now", so a migrated
  slice takes injected time and its tests are deterministic (``docs/adr/007`` §4);
* :class:`~feature_pipeline.ports.clock.RunIdFactory` — run-identifier minting, kept
  byte-compatible with ``pipeline_core.state.Run.create``.

The duplicate ``_now`` / ``_utcnow`` helpers scattered across ``pipeline_core`` are removed as
their callers migrate onto these ports in later phases.

Standard library only.
"""

from __future__ import annotations

from .clock import (
    TIMESTAMP_FORMAT,
    Clock,
    ClockRunIdFactory,
    ManualClock,
    RunIdFactory,
    SystemClock,
    format_timestamp,
)

__all__ = [
    "TIMESTAMP_FORMAT",
    "Clock",
    "ClockRunIdFactory",
    "ManualClock",
    "RunIdFactory",
    "SystemClock",
    "format_timestamp",
]
