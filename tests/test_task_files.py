"""Tests for the normalized execution task contract (EDF-01).

Three layers:
* the immutable ``CommandSpec`` / ``TaskSpec`` contracts in :mod:`schemas.contracts` and their
  fail-closed validation;
* :func:`pipeline_core.task_files.load_task_spec` — parsing a native Markdown task file's
  ``## Execution Metadata`` and ``## Acceptance Criteria`` blocks, and synthesising
  ``AC-1..AC-n`` from a historical ``## Definition of Done`` through caller-supplied defaults
  without touching the source file;
* :func:`pipeline_core.legacy_adapter.adapt_task_spec` — the same normalization for a parsed
  JSON plan entry, so a Markdown and a JSON execution input land on an equivalent ``TaskSpec``.
"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from schemas import SchemaError
from schemas.contracts import AcceptanceCriterionSpec, CommandSpec, TaskSpec

from pipeline_core.legacy_adapter import adapt_task_spec
from pipeline_core.task_files import (
    TaskDefaults,
    TaskFileError,
    load_task_spec,
    synthesize_acceptance_criteria,
)


DECLARED_TASK_MD = """\
# AB-01 - Normalize the execution task contract

## Status
- [ ] To Do
- [x] In Progress
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
- [ ] AC-1 — Inputs normalize to equivalent TaskSpec values.
- [ ] AC-2 — Invalid metadata fails before a run artifact exists.
"""

HISTORICAL_TASK_MD = """\
# LT-09 - Wire the legacy exporter

## Status
- [ ] To Do
- [ ] In Progress
- [ ] Done

## Purpose
x

## Affected Files / Components
- `tools/export/writer.py`
- `tools/export/schema.py`

## Definition of Done
- [ ] Exporter emits v1 records
- [ ] Round-trip test passes
"""

HISTORICAL_DEFAULTS = TaskDefaults(
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


def _semantic(spec: TaskSpec) -> dict[str, object]:
    """The fields a Markdown and a JSON input must agree on, ignoring provenance."""
    return {
        "id": spec.id,
        "task_type": spec.task_type,
        "executor": spec.executor,
        "depends_on": spec.depends_on,
        "allowed_scope": spec.allowed_scope,
        "out_of_scope": spec.out_of_scope,
        "required_skills": spec.required_skills,
        "max_repair_attempts": spec.max_repair_attempts,
        "documentation_impact": spec.documentation_impact,
        "verification_commands": spec.verification_commands,
        "verification_tier": spec.verification_tier,
        "accepts_scoped": spec.accepts_scoped,
        "deferred_verification_commands": spec.deferred_verification_commands,
        "blocking_conditions": spec.blocking_conditions,
        "acceptance_criteria": spec.acceptance_criteria,
    }


class CommandSpecTests(unittest.TestCase):
    def test_from_data_accepts_argv_and_command_string_forms(self) -> None:
        from_argv = CommandSpec.from_data({"cwd": "pkg", "argv": ["pytest", "-q"]})
        from_str = CommandSpec.from_data({"cwd": "pkg", "command": 'pytest "-q"'})
        self.assertEqual(from_argv, CommandSpec("pkg", ("pytest", "-q")))
        self.assertEqual(from_str, CommandSpec("pkg", ("pytest", "-q")))

    def test_shell_operator_in_a_command_fails_closed(self) -> None:
        for argv in (["make", "&&", "install"], ["a", "|", "b"], ["x", ";", "y"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SchemaError):
                    CommandSpec.from_data({"cwd": ".", "argv": argv})

    def test_absolute_working_directory_fails_closed(self) -> None:
        with self.assertRaises(SchemaError):
            CommandSpec.from_data({"cwd": "/etc", "argv": ["true"]})

    def test_command_spec_is_immutable(self) -> None:
        spec = CommandSpec(".", ("true",))
        with self.assertRaises(Exception):
            spec.cwd = "elsewhere"  # type: ignore[misc]


class TaskSpecBuildTests(unittest.TestCase):
    def _complete(self, **overrides: object) -> TaskSpec:
        base: dict[str, object] = dict(
            id="AB-01",
            title="t",
            path="pkg/plans/tasks/AB-01_x.md",
            task_type="python",
            executor="python-executor",
            depends_on=(),
            allowed_scope=("pkg/core/**",),
            out_of_scope=(),
            required_skills=(),
            max_repair_attempts=2,
            documentation_impact=(),
            verification_commands=(CommandSpec("pkg", ("pytest",)),),
            acceptance_criteria=(AcceptanceCriterionSpec("AC-1", "does the thing"),),
        )
        base.update(overrides)
        return TaskSpec.build(**base)  # type: ignore[arg-type]

    def test_a_complete_spec_builds_and_is_frozen(self) -> None:
        spec = self._complete()
        self.assertEqual(spec.id, "AB-01")
        self.assertEqual(spec.verification_tier, "full")
        with self.assertRaises(Exception):
            spec.id = "ZZ-99"  # type: ignore[misc]

    def test_empty_allowed_scope_fails_closed(self) -> None:
        with self.assertRaises(SchemaError):
            self._complete(allowed_scope=())

    def test_unknown_task_type_fails_closed(self) -> None:
        with self.assertRaises(SchemaError):
            self._complete(task_type="refactor")

    def test_absolute_and_traversal_scope_paths_fail_closed(self) -> None:
        for bad in ("C:/x", "/x", "~/x", "a/../../x"):
            with self.subTest(bad=bad):
                with self.assertRaises(SchemaError):
                    self._complete(allowed_scope=(bad,))

    def test_required_skill_must_be_an_exact_skill_md(self) -> None:
        with self.assertRaises(SchemaError):
            self._complete(required_skills=("pkg/skills/testing",))

    def test_negative_repair_limit_fails_closed(self) -> None:
        with self.assertRaises(SchemaError):
            self._complete(max_repair_attempts=-1)

    def test_self_dependency_and_bad_dependency_id_fail_closed(self) -> None:
        with self.assertRaises(SchemaError):
            self._complete(depends_on=("AB-01",))
        with self.assertRaises(SchemaError):
            self._complete(depends_on=("not a task id",))

    def test_acceptance_criteria_ids_must_be_sequential_from_one(self) -> None:
        good = (AcceptanceCriterionSpec("AC-1", "a"), AcceptanceCriterionSpec("AC-2", "b"))
        self.assertEqual(self._complete(acceptance_criteria=good).acceptance_criteria, good)
        for bad in (
            (AcceptanceCriterionSpec("AC-1", "a"), AcceptanceCriterionSpec("AC-3", "b")),
            (AcceptanceCriterionSpec("AC-2", "a"),),
            (AcceptanceCriterionSpec("AC-1", "a"), AcceptanceCriterionSpec("AC-1", "b")),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(SchemaError):
                    self._complete(acceptance_criteria=bad)

    def test_accepts_scoped_must_be_a_declared_dependency(self) -> None:
        with self.assertRaises(SchemaError):
            self._complete(depends_on=("AB-02",), accepts_scoped=("AB-03",))
        spec = self._complete(
            depends_on=("AB-02",), accepts_scoped=("AB-02",), verification_tier="scoped"
        )
        self.assertEqual(spec.accepts_scoped, ("AB-02",))

    def test_deferred_commands_require_the_scoped_tier(self) -> None:
        deferred = (CommandSpec("pkg", ("pytest", "integration")),)
        with self.assertRaises(SchemaError):
            self._complete(deferred_verification_commands=deferred)  # tier defaults to full
        spec = self._complete(
            verification_tier="scoped", deferred_verification_commands=deferred
        )
        self.assertEqual(spec.deferred_verification_commands, deferred)


class LoadTaskSpecMarkdownTests(unittest.TestCase):
    def test_declared_markdown_task_file_normalizes_completely(self) -> None:
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", DECLARED_TASK_MD)
            spec = load_task_spec(path)
        self.assertEqual(spec.id, "AB-01")
        self.assertEqual(spec.title, "Normalize the execution task contract")
        self.assertEqual(spec.task_type, "python")
        self.assertEqual(spec.executor, "python-executor")
        self.assertEqual(spec.depends_on, ())
        self.assertEqual(spec.allowed_scope, ("pkg/core/task_files.py", "pkg/tests/**"))
        self.assertEqual(spec.out_of_scope, ("pkg/core/state.py",))
        self.assertEqual(
            spec.required_skills,
            (".agents/skills/software-development/test-driven-development/SKILL.md",),
        )
        self.assertEqual(
            spec.verification_commands,
            (
                CommandSpec("pkg", ("python", "-m", "unittest", "discover", "-s", "tests", "-t", ".")),
                CommandSpec(".", ("git", "diff", "--check")),
            ),
        )
        self.assertEqual(spec.blocking_conditions, None)
        self.assertEqual([c.id for c in spec.acceptance_criteria], ["AC-1", "AC-2"])
        self.assertEqual(spec.metadata_source, "declared")
        self.assertEqual(spec.defaults_applied, ())

    def test_declared_block_missing_a_required_field_fails_closed(self) -> None:
        broken = DECLARED_TASK_MD.replace("- Executor: python-executor\n", "")
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", broken)
            with self.assertRaises(TaskFileError):
                load_task_spec(path)

    def test_shell_operator_in_a_declared_verification_command_fails_closed(self) -> None:
        broken = DECLARED_TASK_MD.replace(
            "  - `.` -> `git diff --check`\n",
            "  - `.` -> `git add -A && git commit`\n",
        )
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", broken)
            with self.assertRaises(TaskFileError):
                load_task_spec(path)

    def test_non_sequential_acceptance_criteria_fail_closed(self) -> None:
        broken = DECLARED_TASK_MD.replace("AC-2 —", "AC-3 —")
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", broken)
            with self.assertRaises(TaskFileError):
                load_task_spec(path)

    def test_historical_block_less_file_without_defaults_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "LT-09_legacy.md", HISTORICAL_TASK_MD)
            with self.assertRaises(TaskFileError):
                load_task_spec(path)


class LoadTaskSpecHistoricalTests(unittest.TestCase):
    def test_defaults_fill_a_block_less_file_and_the_source_is_not_rewritten(self) -> None:
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "LT-09_legacy.md", HISTORICAL_TASK_MD)
            before = path.read_bytes()
            spec = load_task_spec(path, defaults=HISTORICAL_DEFAULTS)
            after = path.read_bytes()
        self.assertEqual(before, after)  # AC-3: no source-file mutation
        self.assertEqual(spec.metadata_source, "defaults")
        self.assertEqual(spec.task_type, "tooling")
        self.assertEqual(spec.executor, "python-executor")
        self.assertEqual(spec.allowed_scope, ("tools/export/writer.py", "tools/export/schema.py"))
        self.assertEqual(spec.out_of_scope, ("vendor/**",))
        self.assertEqual(spec.verification_commands, (CommandSpec(".", ("make", "check")),))
        self.assertEqual(
            spec.acceptance_criteria,
            (
                AcceptanceCriterionSpec("AC-1", "Exporter emits v1 records", False),
                AcceptanceCriterionSpec("AC-2", "Round-trip test passes", False),
            ),
        )
        self.assertIn("task_type", spec.defaults_applied)
        self.assertIn("acceptance_criteria", spec.defaults_applied)

    def test_a_historical_file_without_a_resolvable_scope_is_rejected(self) -> None:
        no_scope = HISTORICAL_TASK_MD.replace(
            "## Affected Files / Components\n- `tools/export/writer.py`\n- `tools/export/schema.py`\n",
            "",
        )
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "LT-10_legacy.md", no_scope)
            with self.assertRaises(TaskFileError):
                load_task_spec(path, defaults=HISTORICAL_DEFAULTS)

    def test_synthesize_acceptance_criteria_numbers_from_one(self) -> None:
        criteria = synthesize_acceptance_criteria(
            ["first", {"text": "second", "checked": True}, "third"]
        )
        self.assertEqual([c.id for c in criteria], ["AC-1", "AC-2", "AC-3"])
        self.assertTrue(criteria[1].checked)
        with self.assertRaises(SchemaError):
            synthesize_acceptance_criteria(["ok", "  "])


class MarkdownJsonEquivalenceTests(unittest.TestCase):
    """AC-1: a Markdown task file and an equivalent JSON plan entry normalize alike."""

    def _json_entry(self) -> dict[str, object]:
        return {
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
                {"cwd": "pkg", "argv": ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."]},
                {"cwd": ".", "argv": ["git", "diff", "--check"]},
            ],
            "blocking_conditions": None,
            "acceptance_criteria": [
                {"id": "AC-1", "text": "Inputs normalize to equivalent TaskSpec values.", "checked": False},
                {"id": "AC-2", "text": "Invalid metadata fails before a run artifact exists.", "checked": False},
            ],
        }

    def test_markdown_and_json_execution_inputs_are_equivalent(self) -> None:
        with TemporaryDirectory() as raw:
            path = _write(Path(raw), "AB-01_normalize.md", DECLARED_TASK_MD)
            md_spec = load_task_spec(path)
        json_spec = adapt_task_spec(self._json_entry())
        self.assertEqual(_semantic(md_spec), _semantic(json_spec))
        self.assertEqual(md_spec.metadata_source, json_spec.metadata_source)

    def test_json_entry_without_execution_metadata_fails_closed_for_execution(self) -> None:
        bare = {"id": "AB-01", "type": "python", "depends_on": []}
        with self.assertRaises(SchemaError):
            adapt_task_spec(bare)  # no has_metadata, no defaults -> fail closed

    def test_json_entry_missing_a_required_declared_field_fails_closed(self) -> None:
        entry = self._json_entry()
        del entry["allowed_scope"]
        with self.assertRaises(SchemaError):
            adapt_task_spec(entry)

    def test_source_mapping_is_not_mutated(self) -> None:
        entry = self._json_entry()
        original = copy.deepcopy(entry)
        adapt_task_spec(entry)
        self.assertEqual(entry, original)


if __name__ == "__main__":
    unittest.main()
