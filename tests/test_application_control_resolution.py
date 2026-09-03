"""DF-02 — controls are validated and resolved at the boundary, before any side effect.

Plan: ``docs/plans/tasks/DF-02_resolved-controls-clocks.md`` (AC-1, AC-2, AC-3),
``docs/adr/007`` §4/§5. Finding F-07 (accepted-but-unapplied controls) and F-08 (validation
too late) — ``tests/test_critical_behavior_characterization.py``.

* **AC-1 / F-08** — :func:`resolve_execute_controls` raises a :class:`DomainError` on a
  negative ``--max-repair-attempts`` with no filesystem, lease, or process touched. The
  matching characterization test proves the *old* path defers that rejection.
* **AC-2** — every resolved control is a :class:`ResolvedControl` carrying its source, ready
  for later plan persistence.
* **AC-3** — :class:`RunIdentityFactory` builds a run's identity from an injected
  :class:`ManualClock`; the run-ID and timestamp are deterministic and match
  ``pipeline_core.state.Run.create``.

Standard library only.
"""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from feature_pipeline.application.controls import (
    ResolvedExecuteControls,
    resolve_execute_controls,
)
from feature_pipeline.application.identity import RunIdentity, RunIdentityFactory
from feature_pipeline.domain.controls import ControlSource
from feature_pipeline.domain.errors import DomainError, InvalidControl, InvalidIdentifier
from feature_pipeline.ports.clock import ManualClock

from pipeline_core import execution as execution_mod
from pipeline_core import state as state_mod


class ResolveExecuteControlsTests(unittest.TestCase):
    def test_default_when_no_override_given(self) -> None:
        resolved = resolve_execute_controls(max_repair_attempts=None)
        self.assertIsInstance(resolved, ResolvedExecuteControls)
        bound = resolved.max_repair_attempts
        self.assertEqual(bound.value, 2)
        self.assertEqual(bound.source, ControlSource.DEFAULT)

    def test_explicit_override_keeps_its_provenance(self) -> None:
        resolved = resolve_execute_controls(max_repair_attempts=5)
        bound = resolved.max_repair_attempts
        self.assertEqual(bound.value, 5)
        self.assertEqual(bound.source, ControlSource.EXPLICIT)
        self.assertEqual(
            resolved.as_records()["max_repair_attempts"],
            {"value": 5, "sourced": "explicit"},
        )

    def test_zero_is_a_valid_explicit_bound(self) -> None:
        resolved = resolve_execute_controls(max_repair_attempts=0)
        self.assertEqual(resolved.max_repair_attempts.value, 0)
        self.assertEqual(resolved.max_repair_attempts.source, ControlSource.EXPLICIT)

    def test_negative_override_is_rejected_at_the_boundary(self) -> None:
        with self.assertRaises(DomainError) as caught:
            resolve_execute_controls(max_repair_attempts=-3)
        self.assertIsInstance(caught.exception, InvalidControl)
        self.assertEqual(caught.exception.code, "invalid-control")

    def test_rejection_happens_before_state_lease_or_launch(self) -> None:
        # No run directory, no lease file, no subprocess: the call is pure validation. We
        # assert structurally that the old path defers this (F-08) while the new one does not.
        old = inspect.getsource(execution_mod.execute_run)
        self.assertIn("_apply_repair_bound(", old)
        new = inspect.getsource(resolve_execute_controls)
        self.assertNotIn("RunLifecycle", new)
        self.assertNotIn("pipeline_lease", new)
        self.assertNotIn("subprocess", new)


class RunIdentityFactoryTests(unittest.TestCase):
    def test_identity_is_deterministic_under_a_manual_clock(self) -> None:
        clock = ManualClock(datetime(2026, 9, 3, 17, 30, 45, tzinfo=timezone.utc))
        factory = RunIdentityFactory(clock)

        first = factory.create("feature-pipeline")
        second = factory.create("feature-pipeline")

        self.assertIsInstance(first, RunIdentity)
        self.assertEqual(first, second)
        self.assertEqual(first.created_at, "2026-09-03T17:30:45Z")
        self.assertEqual(first.run_id, "2026-09-03T17-30-45Z-feature-pipeline")

    def test_run_id_matches_pipeline_core_run_create(self) -> None:
        fixed = datetime(2026, 9, 3, 17, 30, 45, tzinfo=timezone.utc)
        mine = RunIdentityFactory(ManualClock(fixed)).create("feature-pipeline")

        with patch("pipeline_core.state._now", return_value="2026-09-03T17:30:45Z"):
            run = state_mod.Run.create(
                "feature-pipeline", "prompt.md", None, "run_dir", "."
            )
        self.assertEqual(mine.run_id, run.run_id)

    def test_advancing_the_clock_changes_the_identity(self) -> None:
        clock = ManualClock(datetime(2026, 9, 3, 17, 30, 45, tzinfo=timezone.utc))
        factory = RunIdentityFactory(clock)
        before = factory.create("demo")
        clock.advance(61)
        after = factory.create("demo")
        self.assertNotEqual(before.run_id, after.run_id)
        self.assertEqual(after.created_at, "2026-09-03T17:31:46Z")

    def test_an_invalid_feature_slug_is_rejected(self) -> None:
        factory = RunIdentityFactory(ManualClock())
        with self.assertRaises(InvalidIdentifier):
            factory.create("bad slug/with-space")

    def test_no_real_time_call_below_the_factory(self) -> None:
        src = inspect.getsource(RunIdentityFactory)
        self.assertNotIn("datetime.now", src)
        self.assertNotIn("time.time", src)


if __name__ == "__main__":
    unittest.main()
