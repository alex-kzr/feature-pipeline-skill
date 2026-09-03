"""IN-01 — one input-neutral task definition and a single builder.

Every supported plan source (a native Markdown task file, a JSON plan entry, a historical
block-less file, a parsed legacy mapping) is normalized through
:class:`feature_pipeline.inputs.TaskDefinitionBuilder` into one immutable
:class:`feature_pipeline.domain.models.TaskDefinition`. These tests pin:

* AC-1 — equivalent source documents produce equal normalized definitions (``.semantic``);
* AC-2 — one metadata/command parser: the same field vocabulary and the same shell-free
  command rules reject the same inputs regardless of source;
* AC-3 — the legacy entry points (:func:`pipeline_core.task_files.load_task_spec`,
  :func:`pipeline_core.legacy_adapter.adapt_task_spec`) delegate to the builder and still
  return byte-identical :class:`~feature_pipeline.contracts.TaskSpec` values.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from feature_pipeline.contracts import (
    AcceptanceCriterionSpec,
    CommandSpec,
    SchemaError,
    TaskSpec,
)
from feature_pipeline.domain.models import TaskDefinition
from feature_pipeline.inputs import (
    SourceDefaults,
    TaskDefinitionBuilder,
    parse_markdown_command_line,
    parse_markdown_metadata_block,
)

from pipeline_core.legacy_adapter import adapt_task_spec
from pipeline_core.task_files import load_task_spec


DECLARED_TASK_MD = """\
# AB-01 - Normalize the execution task contract

## Status
- [ ] To Do
- [ ] In Progress
- [ ] Done

## Execution Metadata
- Type: python
- Executor: python-executor
- Depends on: none
- Allowed scope: `pkg/core/task_files.py`, `pkg/tests/**`
- Out of scope: `pkg/core/state.py`
- Required skills: `.agents/skills/software-development/test-driven-development/SKILL.md`
- Maximum repair attempts: 2
- Documentation impact: none
- Verification commands:
  - `pkg` -> `python -m unittest discover -s tests -t .`
  - `.` -> `git diff --check`
- Blocking conditions: none

## Purpose
x

## Acceptance Criteria
- [ ] AC-1 — Inputs normalize to equivalent TaskDefinition values.
- [ ] AC-2 — Invalid metadata fails before a run artifact exists.
"""

EQUIVALENT_JSON_ENTRY: dict[str, object] = {
    "id": "AB-01",
    "title": "Normalize the execution task contract",
    "path": "pkg/plans/tasks/AB-01_normalize.md",
    "has_metadata": True,
    "type": "python",
    "executor": "python-executor",
    "depends_on": [],
    "allowed_scope": ["pkg/core/task_files.py", "pkg/tests/**"],
    "out_of_scope": ["pkg/core/state.py"],
    "required_skills": [
        ".agents/skills/software-development/test-driven-development/SKILL.md"
    ],
    "max_repair_attempts": 2,
    "documentation_impact": [],
    "verification_commands": [
        {"cwd": "pkg", "command": "python -m unittest discover -s tests -t ."},
        {"cwd": ".", "argv": ["git", "diff", "--check"]},
    ],
    "blocking_conditions": None,
    "acceptance_criteria": [
        {"id": "AC-1", "text": "Inputs normalize to equivalent TaskDefinition values.",
         "checked": False},
        {"id": "AC-2", "text": "Invalid metadata fails before a run artifact exists.",
         "checked": False},
    ],
}

HISTORICAL_TASK_MD = """\
# LT-09 - Wire the legacy exporter

## Status
- [ ] To Do

## Purpose
x

## Affected Files / Components
- `tools/export/writer.py`
- `tools/export/schema.py`

## Definition of Done
- [ ] Exporter emits v1 records
- [ ] Round-trip test passes
"""

DEFAULTS = SourceDefaults(
    task_type="tooling",
    executor="python-executor",
    out_of_scope=("vendor/**",),
    documentation_impact=("docs/changelog.md",),
    verification_commands=(CommandSpec(".", ("make", "check")),),
)


def _write(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


class MetadataParserTests(unittest.TestCase):
    """AC-2: one metadata parser, shared field vocabulary and denials."""

    def test_absent_block_returns_none(self) -> None:
        self.assertIsNone(parse_markdown_metadata_block("# T-01 - x\n\n## Purpose\nx\n"))

    def test_unknown_field_fails_closed(self) -> None:
        text = DECLARED_TASK_MD.replace(
            "- Blocking conditions: none\n", "- Blocking conditions: none\n- Priority: high\n"
        )
        with self.assertRaises(SchemaError):
            parse_markdown_metadata_block(text)

    def test_duplicate_field_fails_closed(self) -> None:
        text = DECLARED_TASK_MD.replace(
            "- Executor: python-executor\n",
            "- Executor: python-executor\n- Executor: other-executor\n",
        )
        with self.assertRaises(SchemaError):
            parse_markdown_metadata_block(text)

    def test_command_line_parser_rejects_a_malformed_reference(self) -> None:
        self.assertEqual(
            parse_markdown_command_line("  - `pkg` -> `pytest -q`"), ("pkg", "pytest -q")
        )
        self.assertIsNone(parse_markdown_command_line("  - pkg pytest"))


class BuilderEquivalenceTests(unittest.TestCase):
    """AC-1: a Markdown task file and an equivalent JSON entry normalize alike."""

    def _markdown_definition(self) -> TaskDefinition:
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", DECLARED_TASK_MD)
            return TaskDefinitionBuilder.from_markdown_task_file(path)

    def test_markdown_and_json_definitions_are_semantically_equal(self) -> None:
        md = self._markdown_definition()
        js = TaskDefinitionBuilder.from_json_plan_entry(EQUIVALENT_JSON_ENTRY)
        self.assertEqual(md.semantic, js.semantic)
        self.assertEqual(md.metadata_source, "declared")
        self.assertEqual(js.metadata_source, "declared")
        self.assertNotEqual(md.source_format, js.source_format)

    def test_definition_is_immutable_and_exposes_a_task_spec(self) -> None:
        md = self._markdown_definition()
        self.assertIsInstance(md.to_task_spec(), TaskSpec)
        with self.assertRaises(Exception):
            md.source_format = "other"  # type: ignore[misc]

    def test_command_forms_normalize_to_one_shape(self) -> None:
        md = self._markdown_definition()
        js = TaskDefinitionBuilder.from_json_plan_entry(EQUIVALENT_JSON_ENTRY)
        self.assertEqual(
            md.to_task_spec().verification_commands,
            (
                CommandSpec("pkg", ("python", "-m", "unittest", "discover",
                                    "-s", "tests", "-t", ".")),
                CommandSpec(".", ("git", "diff", "--check")),
            ),
        )
        self.assertEqual(
            md.to_task_spec().verification_commands,
            js.to_task_spec().verification_commands,
        )


class BuilderDenialParityTests(unittest.TestCase):
    """AC-2: the same malformed input is rejected the same way from either source."""

    def _json(self, **changes: object) -> dict[str, object]:
        entry = dict(EQUIVALENT_JSON_ENTRY)
        entry.update(changes)
        return entry

    def test_shell_operator_in_a_command_fails_closed_from_both_sources(self) -> None:
        bad_md = DECLARED_TASK_MD.replace(
            "  - `.` -> `git diff --check`\n", "  - `.` -> `git add -A && git commit`\n"
        )
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", bad_md)
            with self.assertRaises(SchemaError):
                TaskDefinitionBuilder.from_markdown_task_file(path)
        with self.assertRaises(SchemaError):
            TaskDefinitionBuilder.from_json_plan_entry(
                self._json(verification_commands=[
                    {"cwd": ".", "command": "git add -A && git commit"}
                ])
            )

    def test_missing_required_declared_field_fails_closed_from_both_sources(self) -> None:
        broken_md = DECLARED_TASK_MD.replace("- Executor: python-executor\n", "")
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", broken_md)
            with self.assertRaises(SchemaError):
                TaskDefinitionBuilder.from_markdown_task_file(path)
        entry = self._json()
        del entry["executor"]
        with self.assertRaises(SchemaError):
            TaskDefinitionBuilder.from_json_plan_entry(entry)

    def test_non_sequential_acceptance_criteria_fail_closed_from_both_sources(self) -> None:
        broken_md = DECLARED_TASK_MD.replace("AC-2 —", "AC-3 —")
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", broken_md)
            with self.assertRaises(SchemaError):
                TaskDefinitionBuilder.from_markdown_task_file(path)
        with self.assertRaises(SchemaError):
            TaskDefinitionBuilder.from_json_plan_entry(
                self._json(acceptance_criteria=[
                    {"id": "AC-1", "text": "a"}, {"id": "AC-3", "text": "b"}
                ])
            )

    def test_traversal_scope_path_fails_closed_from_both_sources(self) -> None:
        bad_md = DECLARED_TASK_MD.replace("`pkg/core/task_files.py`", "`../outside.py`")
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", bad_md)
            with self.assertRaises(SchemaError):
                TaskDefinitionBuilder.from_markdown_task_file(path)
        with self.assertRaises(SchemaError):
            TaskDefinitionBuilder.from_json_plan_entry(
                self._json(allowed_scope=["../outside.py"])
            )


class HistoricalAndDefaultsTests(unittest.TestCase):
    """AC-1/AC-3: block-less sources take shared defaults and record provenance."""

    def test_block_less_markdown_and_bare_json_entry_agree(self) -> None:
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "LT-09_legacy.md", HISTORICAL_TASK_MD)
            before = path.read_bytes()
            md = TaskDefinitionBuilder.from_markdown_task_file(path, defaults=DEFAULTS)
            self.assertEqual(before, path.read_bytes())  # AC-3: source not rewritten
        js = TaskDefinitionBuilder.from_json_plan_entry(
            {
                "id": "LT-09",
                "title": "Wire the legacy exporter",
                "affected_files": ["tools/export/writer.py", "tools/export/schema.py"],
                "definition_of_done": ["Exporter emits v1 records", "Round-trip test passes"],
            },
            defaults=DEFAULTS,
        )
        self.assertEqual(md.metadata_source, "defaults")
        self.assertEqual(js.metadata_source, "defaults")
        self.assertEqual(md.semantic, js.semantic)
        # provenance labels still follow the source shape (pre-IN-01 behaviour, kept)
        self.assertIn("task_type", md.defaults_applied)
        self.assertIn("acceptance_criteria", md.defaults_applied)
        self.assertIn("type", js.defaults_applied)
        self.assertIn("acceptance_criteria", js.defaults_applied)

    def test_block_less_without_defaults_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "LT-09_legacy.md", HISTORICAL_TASK_MD)
            with self.assertRaises(SchemaError):
                TaskDefinitionBuilder.from_markdown_task_file(path)
        with self.assertRaises(SchemaError):
            TaskDefinitionBuilder.from_json_plan_entry(
                {"id": "LT-09", "definition_of_done": ["x"]}
            )


class LegacyEntryPointDelegationTests(unittest.TestCase):
    """AC-3: the shipped entry points now go through the builder without changing results."""

    def test_load_task_spec_matches_the_builder_spec(self) -> None:
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", DECLARED_TASK_MD)
            facade = load_task_spec(path)
            builder = TaskDefinitionBuilder.from_markdown_task_file(path).to_task_spec()
        self.assertEqual(facade, builder)

    def test_adapt_task_spec_matches_the_builder_spec(self) -> None:
        facade = adapt_task_spec(EQUIVALENT_JSON_ENTRY)
        builder = TaskDefinitionBuilder.from_json_plan_entry(
            EQUIVALENT_JSON_ENTRY
        ).to_task_spec()
        self.assertEqual(facade, builder)

    def test_markdown_facade_and_json_facade_still_agree_semantically(self) -> None:
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", DECLARED_TASK_MD)
            md_spec = load_task_spec(path)
        json_spec = adapt_task_spec(EQUIVALENT_JSON_ENTRY)
        for field in (
            "task_type", "executor", "depends_on", "allowed_scope", "out_of_scope",
            "required_skills", "max_repair_attempts", "documentation_impact",
            "verification_commands", "verification_tier", "accepts_scoped",
            "deferred_verification_commands", "blocking_conditions", "acceptance_criteria",
            "metadata_source",
        ):
            self.assertEqual(getattr(md_spec, field), getattr(json_spec, field), field)


if __name__ == "__main__":
    unittest.main()
