"""Small, repeated assertion helpers shared across the suite (QG-01)."""

from __future__ import annotations

from pipeline_core.adapters import LaunchResult


def assert_launch_ok(testcase, result: LaunchResult) -> None:
    """Assert ``result`` is a successful (``exit_code == 0``) :class:`LaunchResult`."""
    testcase.assertIsInstance(result, LaunchResult)
    testcase.assertEqual(result.exit_code, 0, result.stderr or result.stdout)


def assert_repair_report_numbers(testcase, actual: list[int], expected: list[int]) -> None:
    """Assert the durable repair-report attempt numbers on disk match ``expected`` exactly."""
    testcase.assertEqual(actual, expected)
