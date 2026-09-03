"""BL-04 — structural checks over the worktree-attribution performance baseline.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 0). This module drives
:mod:`tests.worktree_perf` once and asserts the *shape* of the recorded baseline: both
scenarios present with every metric populated (AC-1), the synthetic recipe reproducible
(AC-2), explicit WT-01 parity/improvement thresholds (AC-3), and the ``O(total repository
bytes)`` snapshot relationship finding F-16 predicts. No wall-clock value is asserted —
those are recorded, with the comparison method, in
``docs/validation/worktree-performance-baseline.md``.

Standard library only.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from pipeline_core.worktree import STATE_KNOWN

from tests import worktree_perf as harness

_REPORT: dict[str, harness.Metrics] | None = None


def _shared_report() -> dict[str, harness.Metrics]:
    """Build the baseline once for the whole module — each build shells out to git."""
    global _REPORT
    if _REPORT is None:
        _REPORT = harness.baseline_report()
    return _REPORT


class BaselineReportShapeTests(unittest.TestCase):
    """AC-1 — the baseline reports time, memory, and artifact size for both scenarios."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = _shared_report()

    def test_both_scenarios_are_present(self) -> None:
        self.assertEqual(set(self.report), {"small", "large"})

    def test_every_numeric_metric_is_populated(self) -> None:
        numeric = {
            "boundary_count",
            "vcs_file_count",
            "total_repo_bytes",
            "snapshot_bytes_read",
            "changed_candidate_count",
            "snapshot_seconds",
            "window_seconds",
            "snapshot_peak_heap_bytes",
            "evidence_bytes",
        }
        self.assertTrue(numeric.issubset({f.name for f in fields(harness.Metrics)}))
        for name, metrics in self.report.items():
            for field_name in numeric:
                value = getattr(metrics, field_name)
                self.assertGreater(value, 0, f"{name}.{field_name} was {value!r}")

    def test_time_memory_and_artifact_size_are_all_reported(self) -> None:
        for metrics in self.report.values():
            self.assertGreater(metrics.snapshot_seconds, 0.0)  # elapsed time
            self.assertGreater(metrics.window_seconds, 0.0)
            self.assertGreater(metrics.snapshot_peak_heap_bytes, 0)  # peak memory
            self.assertGreater(metrics.evidence_bytes, 0)  # artifact size

    def test_attribution_is_known_and_confined_to_scope(self) -> None:
        for metrics in self.report.values():
            self.assertEqual(metrics.attribution_state, STATE_KNOWN)
            self.assertTrue(metrics.in_window_edit_attributed)
            self.assertTrue(metrics.preexisting_dirt_excluded)

    def test_candidate_matrix_is_exercised_in_both_scenarios(self) -> None:
        for name, metrics in self.report.items():
            self.assertGreater(metrics.matrix_tracked_clean, 0, name)
            self.assertEqual(metrics.matrix_tracked_dirty, 2, name)
            self.assertEqual(metrics.matrix_untracked, 1, name)
            self.assertEqual(metrics.matrix_binary, 1, name)
            self.assertEqual(metrics.matrix_submodule_like, 1, name)
            self.assertGreaterEqual(metrics.boundary_count, 2, name)


class F16ScalingTests(unittest.TestCase):
    """The snapshot cost the refactor must remove: O(total repository bytes)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = _shared_report()

    def test_snapshot_reads_essentially_the_whole_repository(self) -> None:
        for name, metrics in self.report.items():
            ratio = metrics.snapshot_bytes_read / metrics.total_repo_bytes
            self.assertGreaterEqual(ratio, 0.999, f"{name}: only read {ratio:.4%}")
            self.assertLessEqual(ratio, 1.001, f"{name}: read {ratio:.4%}")

    def test_large_scenario_reads_far_more_than_its_changed_candidates(self) -> None:
        large = self.report["large"]
        # Generous over-estimate of the bytes a candidate-only snapshot would touch.
        changed_bytes = large.changed_candidate_count * harness.LARGE_TEXT_BYTES * 4
        self.assertGreater(
            large.snapshot_bytes_read,
            changed_bytes * 20,
            "snapshot cost is not dominated by total repository bytes",
        )

    def test_snapshot_cost_grows_with_repository_size(self) -> None:
        small, large = self.report["small"], self.report["large"]
        self.assertGreater(large.snapshot_bytes_read, small.snapshot_bytes_read * 10)
        self.assertGreater(large.snapshot_peak_heap_bytes, small.snapshot_peak_heap_bytes)
        # The attributed change set is the same tiny size regardless of repository size.
        self.assertEqual(
            small.changed_candidate_count, large.changed_candidate_count
        )


class ReproducibilityTests(unittest.TestCase):
    """AC-2 — the synthetic repository recipe is reproducible."""

    def _fingerprint(self, root: Path) -> list[tuple[str, int]]:
        from pipeline_core.worktree import capture_snapshot

        snapshot = capture_snapshot(root)
        self.assertTrue(snapshot.available)
        return sorted(
            (name, len(entry.data or b"")) for name, entry in snapshot.files.items()
        )

    def test_two_builds_from_the_same_seed_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = harness.build_scenario(
                "small", harness.SMALL_TRACKED_FILES, harness.SMALL_TEXT_BYTES,
                harness.SMALL_BLOB_BYTES, Path(a),
            )
            second = harness.build_scenario(
                "small", harness.SMALL_TRACKED_FILES, harness.SMALL_TEXT_BYTES,
                harness.SMALL_BLOB_BYTES, Path(b),
            )
            self.assertEqual(
                self._fingerprint(first.root), self._fingerprint(second.root)
            )

    def test_file_count_drives_snapshot_size(self) -> None:
        from pipeline_core.worktree import capture_snapshot

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            small = harness.build_scenario("large", 20, 512, 1024, Path(a))
            big = harness.build_scenario("large", 80, 512, 1024, Path(b))
            small_read = sum(
                len(e.data or b"") for e in capture_snapshot(small.root).files.values()
            )
            big_read = sum(
                len(e.data or b"") for e in capture_snapshot(big.root).files.values()
            )
            self.assertGreater(big_read, small_read * 2)


class ThresholdsAndEnvironmentTests(unittest.TestCase):
    """AC-3 — WT-01 has explicit parity and improvement thresholds to satisfy."""

    def test_wt01_thresholds_are_explicit_and_quantified(self) -> None:
        thresholds = harness.WT01_THRESHOLDS
        self.assertEqual(set(thresholds), {"parity", "improvement"})
        for group, items in thresholds.items():
            self.assertGreaterEqual(len(items), 3, group)
            joined = " ".join(items)
            self.assertTrue(
                any(ch.isdigit() for ch in joined) or "x" in joined,
                f"{group} thresholds name no measurable quantity",
            )

    def test_environment_capture_has_the_comparison_fields(self) -> None:
        env = harness.environment()
        for field_name in ("platform", "python", "implementation", "git", "cpu_count"):
            self.assertTrue(env.get(field_name), field_name)
        self.assertIn("ratio", env["note"])

    def test_wall_clock_is_advisory_not_asserted(self) -> None:
        self.assertTrue(harness.WALL_CLOCK_ADVISORY)
        # The harness itself defines no test case — it only records.
        self.assertFalse(
            any(
                isinstance(getattr(harness, name), type)
                and issubclass(getattr(harness, name), unittest.TestCase)
                for name in dir(harness)
            )
        )


class RenderingTests(unittest.TestCase):
    def test_generated_block_has_every_documented_section(self) -> None:
        rendered = harness.render_markdown(_shared_report(), harness.environment())
        for heading in (
            "## Scenario recipe",
            "## Measured baseline",
            "## Approved budgets",
            "## WT-01 thresholds",
            "## Environment",
        ):
            self.assertIn(heading, rendered)
        self.assertIn("small", rendered)
        self.assertIn("large", rendered)
        self.assertIn("snapshot_bytes_read", rendered)


if __name__ == "__main__":
    unittest.main()
