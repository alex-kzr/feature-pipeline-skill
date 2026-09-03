"""DF-02 — provenance-bearing resolved controls and the validated repair bound.

Plan: ``docs/plans/tasks/DF-02_resolved-controls-clocks.md`` (AC-1, AC-2), ``docs/adr/007``
§4 (control lifecycle) and §5 (validate before side effects).

* **AC-2** — :class:`ResolvedControl` carries the resolved value *and* its source
  (``explicit`` / ``default``), the two source labels ``pipeline_core`` already persists
  (``pipeline_core.state.Run.set_control`` / ``pipeline_core.execution._controls_map``).
* **AC-1 / F-08** — :class:`RepairBound` rejects a negative or non-integer bound with a
  :class:`DomainError`, the same inputs ``feature_pipeline.contracts.TaskSpec.build`` rejects
  with ``SchemaError``, and it maps to CLI exit ``30`` through the one
  :class:`ResultMapper`.

Standard library only.
"""

from __future__ import annotations

import unittest

from feature_pipeline.application.results import Outcome, ResultMapper
from feature_pipeline.contracts import SchemaError, TaskSpec
from feature_pipeline.domain.controls import ControlSource, ResolvedControl
from feature_pipeline.domain.errors import DomainError, InvalidControl
from feature_pipeline.domain.repair import DEFAULT_MAX_REPAIR_ATTEMPTS, RepairBound

from pipeline_core import execution as execution_mod


class ControlSourceTests(unittest.TestCase):
    def test_values_match_the_labels_pipeline_core_persists(self) -> None:
        self.assertEqual(str(ControlSource.EXPLICIT), "explicit")
        self.assertEqual(str(ControlSource.DEFAULT), "default")

    def test_the_labels_appear_in_the_live_controls_map(self) -> None:
        import inspect

        src = inspect.getsource(execution_mod._controls_map)
        self.assertIn('"explicit"', src)
        self.assertIn('"default"', src)


class ResolvedControlTests(unittest.TestCase):
    def test_retains_value_and_provenance(self) -> None:
        explicit: ResolvedControl[int] = ResolvedControl(
            "max_repair_attempts", 5, ControlSource.EXPLICIT
        )
        self.assertEqual(explicit.value, 5)
        self.assertTrue(explicit.explicit)
        self.assertFalse(explicit.is_default)

        default: ResolvedControl[str] = ResolvedControl(
            "adapter", "claude", ControlSource.DEFAULT
        )
        self.assertTrue(default.is_default)
        self.assertFalse(default.explicit)

    def test_is_frozen(self) -> None:
        control: ResolvedControl[int] = ResolvedControl("x", 1, ControlSource.DEFAULT)
        with self.assertRaises(Exception):
            control.value = 2  # type: ignore[misc]

    def test_as_record_is_the_set_control_shape(self) -> None:
        control: ResolvedControl[int] = ResolvedControl(
            "max_repair_attempts", 3, ControlSource.EXPLICIT
        )
        self.assertEqual(
            control.as_record(), {"value": 3, "sourced": "explicit"}
        )

    def test_resolve_picks_source_from_whether_an_explicit_value_was_given(self) -> None:
        given: ResolvedControl[int] = ResolvedControl.resolve(
            "max_repair_attempts", 4, default=2
        )
        self.assertEqual((given.value, given.source), (4, ControlSource.EXPLICIT))

        omitted: ResolvedControl[int] = ResolvedControl.resolve(
            "max_repair_attempts", None, default=2
        )
        self.assertEqual((omitted.value, omitted.source), (2, ControlSource.DEFAULT))


class RepairBoundTests(unittest.TestCase):
    def test_default_matches_the_taskspec_default(self) -> None:
        self.assertEqual(DEFAULT_MAX_REPAIR_ATTEMPTS, 2)
        self.assertEqual(int(RepairBound.default()), 2)
        # Parity with the schema builder's declared default.
        spec = _spec()
        self.assertEqual(spec.max_repair_attempts, DEFAULT_MAX_REPAIR_ATTEMPTS)

    def test_zero_and_positive_are_accepted(self) -> None:
        self.assertEqual(int(RepairBound(0)), 0)
        self.assertEqual(int(RepairBound(7)), 7)

    def test_a_negative_bound_is_rejected(self) -> None:
        with self.assertRaises(InvalidControl) as caught:
            RepairBound(-3)
        self.assertEqual(caught.exception.code, "invalid-control")
        self.assertIsInstance(caught.exception, DomainError)

    def test_a_boolean_or_non_integer_is_rejected(self) -> None:
        for bad in (True, 2.0, "2", None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidControl):
                    RepairBound(bad)  # type: ignore[arg-type]

    def test_parity_taskspec_build_rejects_the_same_negative_input(self) -> None:
        with self.assertRaises(SchemaError):
            _spec(max_repair_attempts=-3)
        # The message is preserved so a migrated call site reads identically.
        with self.assertRaises(InvalidControl) as caught:
            RepairBound(-3)
        self.assertEqual(
            str(caught.exception), "max_repair_attempts must be a non-negative integer"
        )

    def test_maps_to_cli_exit_30(self) -> None:
        mapper = ResultMapper()
        try:
            RepairBound(-1)
        except DomainError as exc:
            result = mapper.from_domain_error(exc)
        self.assertEqual(result.outcome, Outcome.ERROR)
        self.assertEqual(result.exit_code, 30)


def _spec(**overrides: object) -> TaskSpec:
    base: dict[str, object] = dict(
        id="DF-02",
        title="Resolve controls and clocks at the boundary",
        path="docs/plans/tasks/DF-02_resolved-controls-clocks.md",
        task_type="python",
        executor="python-executor",
        allowed_scope=("in_scope.py",),
        out_of_scope=(),
        required_skills=(),
        verification_commands=(),
    )
    base.update(overrides)
    return TaskSpec.build(**base)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
