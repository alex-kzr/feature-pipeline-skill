"""DF-02 — clock and run-ID ports at the effectful boundary.

Plan: ``docs/plans/tasks/DF-02_resolved-controls-clocks.md`` (AC-3), ``docs/adr/007`` §4.

The portable core carries five identical ``_now`` / ``_utcnow`` helpers
(``pipeline_core.state``, ``pipeline_core.execution``, ``pipeline_core.lease``,
``pipeline_core.concurrency``, ``pipeline_core.diagnostics``), each calling
``datetime.now(timezone.utc)`` directly. DF-02 introduces one :class:`Clock` port and one
run-ID port so the migrated slice takes injected time; the duplicate helpers are removed as
callers migrate in later phases.

* **Parity** — :func:`format_timestamp` reproduces the ``"%Y-%m-%dT%H:%M:%SZ"`` string every
  ``_now`` helper produces, and :class:`ClockRunIdFactory` reproduces
  ``pipeline_core.state.Run.create``'s ``run_id`` composition byte-for-byte.
* **AC-3** — a :class:`ManualClock` makes the boundary deterministic with no real time call.

Standard library only.
"""

from __future__ import annotations

import inspect
import re
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from feature_pipeline.ports.clock import (
    TIMESTAMP_FORMAT,
    Clock,
    ClockRunIdFactory,
    ManualClock,
    RunIdFactory,
    SystemClock,
    format_timestamp,
)

from pipeline_core import execution as execution_mod
from pipeline_core import state as state_mod

_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class SystemClockTests(unittest.TestCase):
    def test_now_is_timezone_aware_utc(self) -> None:
        moment = SystemClock().now()
        self.assertIsNotNone(moment.tzinfo)
        self.assertEqual(moment.utcoffset(), timezone.utc.utcoffset(None))

    def test_satisfies_the_clock_protocol(self) -> None:
        self.assertIsInstance(SystemClock(), Clock)


class ManualClockTests(unittest.TestCase):
    def test_now_is_stable_until_advanced(self) -> None:
        clock = ManualClock(datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc))
        first = clock.now()
        second = clock.now()
        self.assertEqual(first, second)

    def test_advance_moves_time_forward_only_when_told(self) -> None:
        clock = ManualClock(datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc))
        clock.advance(90)
        self.assertEqual(
            clock.now(), datetime(2026, 3, 1, 12, 1, 30, tzinfo=timezone.utc)
        )

    def test_a_naive_start_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ManualClock(datetime(2026, 3, 1, 12, 0, 0))

    def test_satisfies_the_clock_protocol(self) -> None:
        self.assertIsInstance(ManualClock(), Clock)


class FormatTimestampTests(unittest.TestCase):
    def test_matches_the_strftime_pattern_every_now_helper_uses(self) -> None:
        moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.assertEqual(format_timestamp(moment), "2026-01-02T03:04:05Z")
        self.assertRegex(format_timestamp(moment), _STAMP_RE)

    def test_the_format_constant_is_the_literal_used_across_pipeline_core(self) -> None:
        self.assertEqual(TIMESTAMP_FORMAT, "%Y-%m-%dT%H:%M:%SZ")
        # ``pipeline_core.lease`` / ``concurrency`` stopped formatting timestamps at RS-02 -
        # the lock-file stamp now lives in ``feature_pipeline.infrastructure.locks.record``.
        for module in (state_mod, execution_mod):
            self.assertIn(TIMESTAMP_FORMAT, inspect.getsource(module))

    def test_a_non_utc_instant_is_normalised_to_utc(self) -> None:
        from datetime import timedelta

        plus_two = timezone(timedelta(hours=2))
        moment = datetime(2026, 1, 2, 5, 4, 5, tzinfo=plus_two)
        self.assertEqual(format_timestamp(moment), "2026-01-02T03:04:05Z")

    def test_a_naive_instant_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            format_timestamp(datetime(2026, 1, 2, 3, 4, 5))

    def test_parity_with_the_live_state_now_helper(self) -> None:
        # Same production shape: both render the identical strftime pattern.
        self.assertRegex(state_mod._now(), _STAMP_RE)
        self.assertRegex(format_timestamp(datetime.now(timezone.utc)), _STAMP_RE)


class ClockRunIdFactoryTests(unittest.TestCase):
    def test_satisfies_the_run_id_factory_protocol(self) -> None:
        self.assertIsInstance(ClockRunIdFactory(ManualClock()), RunIdFactory)

    def test_run_id_shape_matches_run_create(self) -> None:
        clock = ManualClock(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        run_id = ClockRunIdFactory(clock).new_run_id("demo")
        self.assertEqual(run_id, "2026-01-02T03-04-05Z-demo")

    def test_byte_identical_to_pipeline_core_run_create(self) -> None:
        fixed = datetime(2026, 9, 3, 17, 30, 45, tzinfo=timezone.utc)
        factory_id = ClockRunIdFactory(ManualClock(fixed)).new_run_id("feature-pipeline")

        with patch("pipeline_core.state._now", return_value="2026-09-03T17:30:45Z"):
            run = state_mod.Run.create(
                "feature-pipeline", "prompt.md", None, "run_dir", "."
            )
        self.assertEqual(factory_id, run.run_id)


if __name__ == "__main__":
    unittest.main()
