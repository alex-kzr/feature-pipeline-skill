"""Tests for the non-mutating legacy task metadata adapter."""

from __future__ import annotations

import copy
import unittest

from pipeline_core.legacy_adapter import (
    AcceptanceCriterion,
    AdaptedTask,
    LegacyAdapterError,
    LegacyDefaults,
    adapt_legacy_task,
    synthesize_acceptance_criteria,
)


DEFAULTS = LegacyDefaults(
    task_type="tooling",
    executor="tool-executor",
    out_of_scope=("vendor/**",),
    documentation_impact=("docs/changelog.md",),
    verification_commands=({"cwd": ".", "argv": ["make", "check"]},),
)


class LegacyHistoricalTaskTests(unittest.TestCase):
    def test_block_less_file_takes_defaults_and_is_not_mutated(self) -> None:
        raw = {
            "id": "LT-07",
            "title": "Wire the legacy exporter",
            "affected_files": ["tools/export/**"],
            "depends_on": ["LT-06"],
            "definition_of_done": ["Exporter emits v1 records", "Round-trip test passes"],
        }
        original = copy.deepcopy(raw)

        adapted = adapt_legacy_task(raw, DEFAULTS)

        self.assertEqual(raw, original)  # AC-2: the source mapping is untouched
        self.assertEqual(adapted.metadata_source, "defaults")
        self.assertEqual(adapted.task_type, "tooling")
        self.assertEqual(adapted.executor, "tool-executor")
        self.assertEqual(adapted.allowed_scope, ("tools/export/**",))
        self.assertEqual(adapted.out_of_scope, ("vendor/**",))
        self.assertEqual(adapted.verification_commands, ({"cwd": ".", "argv": ["make", "check"]},))
        self.assertEqual(
            adapted.acceptance_criteria,
            (AcceptanceCriterion("AC-1", "Exporter emits v1 records", False),
             AcceptanceCriterion("AC-2", "Round-trip test passes", False)),
        )
        self.assertIn("type", adapted.defaults_applied)
        self.assertIn("acceptance_criteria", adapted.defaults_applied)

    def test_synthesized_ids_are_sequential_from_one(self) -> None:
        criteria = synthesize_acceptance_criteria(
            ["first", {"text": "second", "checked": True}, "third"]
        )
        self.assertEqual([c.id for c in criteria], ["AC-1", "AC-2", "AC-3"])
        self.assertTrue(criteria[1].checked)

    def test_a_file_without_a_resolvable_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(LegacyAdapterError, "allowed scope"):
            adapt_legacy_task({"id": "LT-08", "definition_of_done": ["x"]}, DEFAULTS)


class DeclaredMetadataTests(unittest.TestCase):
    def _declared(self) -> dict[str, object]:
        return {
            "id": "PO-02",
            "title": "Implement generic stages and legacy metadata adapter",
            "has_metadata": True,
            "type": "python",
            "executor": "python-executor",
            "depends_on": ["PO-01"],
            "allowed_scope": ["feature-pipeline-skill/pipeline_core/**"],
            "out_of_scope": ["feature-pipeline-skill/fixtures/**"],
            "documentation_impact": ["feature-pipeline-skill/README.md"],
            "verification_commands": [{"cwd": "feature-pipeline-skill", "argv": ["git", "diff", "--check"]}],
            "acceptance_criteria": [
                {"id": "AC-1", "text": "Two fixtures execute different stages", "checked": False},
                {"id": "AC-2", "text": "Legacy task files parse through the adapter", "checked": False},
            ],
        }

    def test_declared_values_win_and_no_historical_defaults_are_applied(self) -> None:
        raw = self._declared()
        original = copy.deepcopy(raw)

        adapted = adapt_legacy_task(raw, DEFAULTS)

        self.assertEqual(raw, original)
        self.assertEqual(adapted.metadata_source, "declared")
        self.assertEqual(adapted.task_type, "python")
        self.assertEqual(adapted.executor, "python-executor")
        self.assertEqual(adapted.out_of_scope, ("feature-pipeline-skill/fixtures/**",))
        self.assertEqual(adapted.acceptance_criteria[0].id, "AC-1")
        # Only the contract-permitted absentees may still be defaulted.
        self.assertEqual(adapted.defaults_applied, ("max_repair_attempts",))
        self.assertEqual(adapted.max_repair_attempts, 2)

    def test_declared_block_missing_a_required_field_is_an_error(self) -> None:
        raw = self._declared()
        del raw["executor"]
        with self.assertRaisesRegex(LegacyAdapterError, "required field 'executor'"):
            adapt_legacy_task(raw, DEFAULTS)

    def test_declared_blocking_conditions_survive_and_default_to_none(self) -> None:
        raw = self._declared()
        self.assertIsNone(adapt_legacy_task(raw, DEFAULTS).blocking_conditions)
        raw["blocking_conditions"] = "needs a provisioned database"
        self.assertEqual(
            adapt_legacy_task(raw, DEFAULTS).blocking_conditions, "needs a provisioned database"
        )


if __name__ == "__main__":
    unittest.main()
