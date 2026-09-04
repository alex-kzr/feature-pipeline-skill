"""WT-01 — structural checks and budget assertions over the bounded worktree inspector.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 7). This module drives
:mod:`tests.worktree_perf` once and asserts:

* AC-1 parity — the bounded inspector's attribution of one window matches the retained
  legacy full-snapshot attribution of the *same* window: identical state, identical
  changed-candidate count, identical per-candidate classification, ``evidence_bytes``
  within +/-5%;
* AC-2 — an unavailable/partial attribution never appears as an empty successful manifest;
* AC-3 improvement — ``bytes_read`` is ``O(changed candidates)`` and far below
  ``total_repo_bytes``; ``peak_heap_bytes`` is far below the legacy snapshot peak;
* the synthetic recipe is reproducible (AC-2 of BL-04, carried forward);
* wall-clock is recorded, never asserted as an absolute.

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


class BoundedReportShapeTests(unittest.TestCase):
    """The harness reports time, memory, and artifact size for both scenarios."""

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
            "bytes_read",
            "legacy_bytes_read",
            "changed_candidate_count",
            "snapshot_seconds",
            "window_seconds",
            "legacy_window_seconds",
            "peak_heap_bytes",
            "legacy_peak_heap_bytes",
            "evidence_bytes",
        }
        self.assertTrue(numeric.issubset({f.name for f in fields(harness.Metrics)}))
        for name, metrics in self.report.items():
            for field_name in numeric:
                value = getattr(metrics, field_name)
                self.assertGreater(value, 0, f"{name}.{field_name} was {value!r}")

    def test_candidate_matrix_is_exercised_in_both_scenarios(self) -> None:
        for name, metrics in self.report.items():
            self.assertGreater(metrics.matrix_tracked_clean, 0, name)
            self.assertEqual(metrics.matrix_tracked_dirty, 2, name)
            self.assertEqual(metrics.matrix_untracked, 1, name)
            self.assertEqual(metrics.matrix_binary, 1, name)
            self.assertEqual(metrics.matrix_submodule_like, 1, name)
            self.assertGreaterEqual(metrics.boundary_count, 2, name)


class ParityTests(unittest.TestCase):
    """AC-1 — bounded attribution equals the legacy full-snapshot attribution."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = _shared_report()

    def test_state_and_changed_count_match_legacy(self) -> None:
        for name, m in self.report.items():
            self.assertEqual(m.attribution_state, STATE_KNOWN, name)
            self.assertEqual(m.legacy_attribution_state, STATE_KNOWN, name)
            self.assertEqual(
                m.changed_candidate_count, m.legacy_changed_candidate_count, name
            )

    def test_every_candidate_classification_matches_legacy(self) -> None:
        for name, m in self.report.items():
            self.assertTrue(m.classifications_match, name)

    def test_evidence_bytes_within_five_percent_of_legacy(self) -> None:
        for name, m in self.report.items():
            drift = abs(m.evidence_bytes - m.legacy_evidence_bytes)
            self.assertLessEqual(
                drift,
                0.05 * m.legacy_evidence_bytes,
                f"{name}: evidence_bytes {m.evidence_bytes} vs legacy "
                f"{m.legacy_evidence_bytes}",
            )

    def test_preexisting_dirt_excluded_and_window_edit_attributed(self) -> None:
        for name, m in self.report.items():
            self.assertTrue(m.preexisting_dirt_excluded, name)
            self.assertTrue(m.in_window_edit_attributed, name)


class ImprovementBudgetTests(unittest.TestCase):
    """AC-3 — the bounded inspector meets the BL-04 improvement budgets."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = _shared_report()

    def test_bytes_read_is_order_changed_candidates(self) -> None:
        for name, m in self.report.items():
            budget = 4 * harness.LARGE_TEXT_BYTES * m.changed_candidate_count + 262144
            self.assertLessEqual(m.bytes_read, budget, name)

    def test_large_bytes_read_far_below_total_repo_bytes(self) -> None:
        large = self.report["large"]
        self.assertGreaterEqual(
            large.total_repo_bytes,
            large.bytes_read * 20,
            "bounded snapshot still reads a large fraction of the repository",
        )
        # The legacy snapshot, by contrast, reads essentially the whole repository.
        self.assertGreaterEqual(
            large.legacy_bytes_read, large.total_repo_bytes * 0.9
        )

    def test_large_retained_heap_far_below_legacy(self) -> None:
        large = self.report["large"]
        # The marker retains index metadata + a few small bodies; the legacy snapshot
        # retains every file's bytes. That structural difference is the F-16 memory win.
        self.assertGreaterEqual(
            large.legacy_retained_heap_bytes,
            large.retained_heap_bytes * 10,
            f"bounded retained heap {large.retained_heap_bytes} vs legacy "
            f"{large.legacy_retained_heap_bytes}",
        )

    def test_bounded_window_not_slower_than_legacy_on_this_machine(self) -> None:
        # Ratio comparison only (advisory), and generous — CI noise must not flake it.
        large = self.report["large"]
        self.assertLess(
            large.window_seconds,
            large.legacy_window_seconds * 2.0,
            "bounded window is dramatically slower than the legacy window",
        )

    def test_small_scenario_does_not_regress_versus_legacy(self) -> None:
        small = self.report["small"]
        # A tiny repository: the metadata-first path must not read more than the legacy
        # full snapshot did, plus a small fixed allowance.
        self.assertLessEqual(small.bytes_read, small.legacy_bytes_read + 262144)


class UnavailableNeverEmptyTests(unittest.TestCase):
    """AC-2 — a partial or unavailable attribution is never an empty successful manifest."""

    def test_missing_boundary_is_unavailable_not_empty_known(self) -> None:
        from pipeline_core.reports import launch_artifacts
        from pipeline_core.worktree import (
            STATE_UNAVAILABLE,
            attribute_executor_window,
            capture_snapshot,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)  # not a git repo
            marker = capture_snapshot(root)
            self.assertFalse(marker.available)
            artifacts = launch_artifacts(root / "run", "WT-01", 1)
            result = attribute_executor_window(
                marker, root, artifacts=artifacts, task_id="WT-01",
                allowed_scope=("**",), attempt=1, generation=1,
                executor_report=artifacts.executor_report, exclude_roots=(),
            )
            self.assertEqual(result.state, STATE_UNAVAILABLE)
            self.assertTrue(result.reason)
            self.assertEqual(result.changed_files, [])
            import json as _json

            manifest = _json.loads(
                artifacts.implementation_manifest.read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["attribution_state"], STATE_UNAVAILABLE)
            self.assertIsNotNone(manifest["reason"])


class ReproducibilityTests(unittest.TestCase):
    """AC-2 (BL-04, carried forward) — the synthetic repository recipe is reproducible."""

    def _fingerprint(self, root: Path) -> list[tuple[str, int]]:
        from pipeline_core.worktree import _legacy_capture_snapshot

        snapshot = _legacy_capture_snapshot(root)
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


class ThresholdsAndEnvironmentTests(unittest.TestCase):
    """AC-3 — WT-01 has explicit parity and improvement thresholds."""

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
            "## Measured (bounded vs legacy)",
            "## WT-01 thresholds",
            "## Environment",
        ):
            self.assertIn(heading, rendered)
        self.assertIn("small", rendered)
        self.assertIn("large", rendered)
        self.assertIn("bytes_read", rendered)


if __name__ == "__main__":
    unittest.main()
