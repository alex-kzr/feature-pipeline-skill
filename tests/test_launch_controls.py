"""RS-03 — every accepted launch control is validated, persisted, and applied.

Covers :mod:`feature_pipeline.application.launch_controls` (BL-02 F-07, ADR 007 §4/§5):
an unknown control key is rejected by name, a malformed value is rejected before any launch
object exists, and a resolved control is threaded into each :class:`ResolvedLaunch` — the
concrete timeout, the ``kind`` output budget, and the serialized-program mutex.
"""

from __future__ import annotations

import unittest

from feature_pipeline.application.launch_controls import (
    SUPPORTED_CONTROLS,
    InvalidControl,
    LaunchControls,
    ResolvedLaunch,
    UnsupportedControl,
    launch_scope,
    plan_launch,
    resolve_launch_controls,
)
from pipeline_core.commands import DIAGNOSTIC_OUTPUT_BUDGET, ROUTINE_OUTPUT_BUDGET


class ResolveRejectsUnsupportedAndMalformed(unittest.TestCase):
    def test_unknown_key_is_rejected_by_name(self) -> None:
        with self.assertRaises(UnsupportedControl) as caught:
            resolve_launch_controls({"turbo_mode": True})
        self.assertEqual(caught.exception.key, "turbo_mode")
        self.assertEqual(caught.exception.code, "unsupported-control")

    def test_no_control_is_a_silent_no_op_every_supported_key_round_trips(self) -> None:
        raw = {
            "command_timeout_s": 30.0,
            "routine_output_byte_budget": 4096,
            "diagnostic_output_byte_budget": 8192,
            "serialized_programs": ["cargo", "msbuild"],
        }
        self.assertEqual(set(raw), SUPPORTED_CONTROLS)
        controls = resolve_launch_controls(raw)
        record = controls.as_record()
        self.assertEqual(record["command_timeout_s"], 30.0)
        self.assertEqual(record["routine_output_byte_budget"], 4096)
        self.assertEqual(record["diagnostic_output_byte_budget"], 8192)
        self.assertEqual(record["serialized_programs"], ["cargo", "msbuild"])
        for key in SUPPORTED_CONTROLS:
            self.assertEqual(record["sources"][key], "explicit")

    def test_malformed_values_are_rejected_with_a_named_reason(self) -> None:
        cases = {
            "command_timeout_s": -1,
            "routine_output_byte_budget": -5,
            "diagnostic_output_byte_budget": "big",
            "serialized_programs": "cargo",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                with self.assertRaises(InvalidControl) as caught:
                    resolve_launch_controls({key: value})
                self.assertEqual(caught.exception.key, key)

    def test_serialized_program_may_not_carry_a_path(self) -> None:
        with self.assertRaises(InvalidControl):
            resolve_launch_controls({"serialized_programs": ["bin/cargo"]})

    def test_booleans_are_not_accepted_as_numbers(self) -> None:
        with self.assertRaises(InvalidControl):
            resolve_launch_controls({"routine_output_byte_budget": True})


class DefaultsMatchTheProductionBudgets(unittest.TestCase):
    def test_unset_budgets_resolve_to_the_pipeline_core_constants(self) -> None:
        controls = resolve_launch_controls({})
        self.assertEqual(controls.routine_output_byte_budget, ROUTINE_OUTPUT_BUDGET)
        self.assertEqual(controls.diagnostic_output_byte_budget, DIAGNOSTIC_OUTPUT_BUDGET)
        self.assertIsNone(controls.command_timeout_s)
        self.assertEqual(controls.serialized_programs, ())
        for key in SUPPORTED_CONTROLS:
            self.assertEqual(controls.sources[key], "default")

    def test_explicit_none_timeout_is_recorded_as_an_explicit_choice(self) -> None:
        controls = resolve_launch_controls({"command_timeout_s": None})
        self.assertIsNone(controls.command_timeout_s)
        self.assertEqual(controls.sources["command_timeout_s"], "explicit")


class PlanLaunchThreadsEveryControl(unittest.TestCase):
    def _controls(self) -> LaunchControls:
        return resolve_launch_controls(
            {"command_timeout_s": 45.0, "serialized_programs": ["cargo"]}
        )

    def test_routine_launch_gets_the_routine_budget_and_the_timeout(self) -> None:
        launch = plan_launch(
            self._controls(), argv=("pytest", "-q"), cwd="pkg",
            program_basename="pytest", kind="routine",
        )
        self.assertIsInstance(launch, ResolvedLaunch)
        self.assertEqual(launch.timeout_s, 45.0)
        self.assertEqual(launch.output_byte_budget, ROUTINE_OUTPUT_BUDGET)
        self.assertIsNone(launch.mutex_program)
        self.assertFalse(launch.is_serialized)

    def test_diagnostic_launch_gets_the_diagnostic_budget(self) -> None:
        launch = plan_launch(
            self._controls(), argv=("pytest", "-q"), cwd="pkg",
            program_basename="pytest", kind="diagnostic",
        )
        self.assertEqual(launch.output_byte_budget, DIAGNOSTIC_OUTPUT_BUDGET)

    def test_a_serialized_program_selects_its_mutex_by_lowercased_stem(self) -> None:
        launch = plan_launch(
            self._controls(), argv=("cargo.exe", "build"), cwd=".",
            program_basename="CARGO.EXE", kind="routine",
        )
        self.assertEqual(launch.mutex_program, "cargo")
        self.assertTrue(launch.is_serialized)

    def test_empty_argv_is_rejected_before_a_launch_object_is_built(self) -> None:
        with self.assertRaises(InvalidControl):
            plan_launch(
                resolve_launch_controls({}), argv=(), cwd=".",
                program_basename="x", kind="routine",
            )


class LaunchScopeKeepsCleanupAndEvidenceComplete(unittest.TestCase):
    def _serialized_launch(self) -> ResolvedLaunch:
        controls = resolve_launch_controls({"serialized_programs": ["cargo"]})
        return plan_launch(
            controls, argv=("cargo", "test"), cwd=".",
            program_basename="cargo", kind="routine",
        )

    def test_mutex_is_released_even_when_the_launch_body_raises(self) -> None:
        events: list[str] = []

        class _Mutex:
            def __enter__(self) -> "object":
                events.append("acquire")
                return self

            def __exit__(self, *_exc: object) -> bool:
                events.append("release")
                return False

        with self.assertRaises(RuntimeError):
            with launch_scope(self._serialized_launch(), mutex=_Mutex()):
                events.append("body")
                raise RuntimeError("launch failed")

        self.assertEqual(events, ["acquire", "body", "release"])

    def test_a_non_serialized_launch_needs_no_mutex(self) -> None:
        launch = plan_launch(
            resolve_launch_controls({}), argv=("echo", "hi"), cwd=".",
            program_basename="echo", kind="routine",
        )
        with launch_scope(launch) as scoped:
            self.assertIs(scoped, launch)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
