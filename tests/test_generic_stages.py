"""Tests for project-declared generic stages and the release dry-run contract."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from pipeline_core.stages import (
    BLOCKED,
    FAIL,
    PASS,
    plan_release_dry_run,
    run_tool_stage,
)
from pipeline_core.state import EXIT_NOT_FOUND, Run
from schemas import SchemaError, ToolStage


def _run(root: Path) -> Run:
    prompt = root / "prompt.md"
    prompt.write_text("feature", encoding="utf-8")
    return Run.create("stages", prompt, None, root / "runs" / "stages", root)


def _touch_stage(name: str, output: str) -> ToolStage:
    return ToolStage.from_data({
        "name": name,
        "subagents": ["executor"],
        "argv": [sys.executable, "-c", f"open({output!r}, 'w').close()"],
        "outputs": [output],
    })


class GenericStageTests(unittest.TestCase):
    def test_two_projects_run_different_declared_stages_through_one_code_path(self) -> None:
        # AC-1: the runner never looks at which project it is; the stage data differs, the code
        # does not.
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            root_a, root_b = Path(a), Path(b)
            outcome_a = run_tool_stage(_run(root_a), _touch_stage("build", "library.out"), root_a)
            outcome_b = run_tool_stage(_run(root_b), _touch_stage("render", "incident.html"), root_b)

            self.assertEqual((outcome_a.verdict, outcome_b.verdict), (PASS, PASS))
            self.assertTrue((root_a / "library.out").exists())
            self.assertTrue((root_b / "incident.html").exists())
            self.assertNotEqual(outcome_a.command["argv"], outcome_b.command["argv"])

    def test_absent_prerequisite_blocks_before_the_command_runs(self) -> None:
        ran = []
        stage = ToolStage.from_data({
            "name": "gate", "subagents": ["executor"], "argv": ["noop"],
            "prerequisites": ["expected/input.json"],
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outcome = run_tool_stage(
                _run(root), stage, root,
                command_runner=lambda *a, **k: ran.append(a) or {"exit_code": 0},
            )
        self.assertEqual(outcome.verdict, BLOCKED)
        self.assertEqual(outcome.missing_prerequisites, ("expected/input.json",))
        self.assertEqual(ran, [])

    def test_missing_declared_output_is_a_failure(self) -> None:
        stage = ToolStage.from_data({
            "name": "build", "subagents": ["executor"], "argv": ["noop"],
            "outputs": ["dist/app.js"],
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outcome = run_tool_stage(_run(root), stage, root,
                                     command_runner=lambda *a, **k: {"exit_code": 0})
        self.assertEqual(outcome.verdict, FAIL)
        self.assertEqual(outcome.missing_outputs, ("dist/app.js",))

    def test_unresolvable_program_blocks_rather_than_fails(self) -> None:
        stage = ToolStage.from_data({"name": "x", "subagents": ["executor"], "argv": ["noop"]})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outcome = run_tool_stage(_run(root), stage, root,
                                     command_runner=lambda *a, **k: {"exit_code": EXIT_NOT_FOUND})
        self.assertEqual(outcome.verdict, BLOCKED)

    def test_tracked_empty_diff_policy_fails_on_a_new_tracked_change(self) -> None:
        stage = ToolStage.from_data({
            "name": "graph", "subagents": ["executor"], "argv": ["noop"],
            "diff_policy": "tracked-empty",
        })
        changes: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clean = run_tool_stage(_run(root), stage, root,
                                   command_runner=lambda *a, **k: {"exit_code": 0},
                                   tracked_changes=lambda: list(changes))
            dirty = run_tool_stage(
                _run(root), stage, root,
                command_runner=lambda *a, **k: changes.append("M docs/x.md") or {"exit_code": 0},
                tracked_changes=lambda: list(changes),
            )
        self.assertEqual(clean.verdict, PASS)
        self.assertEqual(dirty.verdict, FAIL)
        self.assertEqual(dirty.unexpected_tracked_changes, ("M docs/x.md",))

    def test_tracked_empty_diff_policy_without_a_probe_blocks(self) -> None:
        stage = ToolStage.from_data({
            "name": "graph", "subagents": ["executor"], "argv": ["noop"],
            "diff_policy": "tracked-empty",
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outcome = run_tool_stage(_run(root), stage, root,
                                     command_runner=lambda *a, **k: {"exit_code": 0})
        self.assertEqual(outcome.verdict, BLOCKED)


class ReleaseDryRunTests(unittest.TestCase):
    def test_dry_run_plans_declared_steps_without_executing_them(self) -> None:
        steps = [
            ToolStage.from_data({"name": "verify", "subagents": ["executor"],
                                 "argv": [sys.executable, "-c", "print('x')"], "timeout": 30}),
            ToolStage.from_data({"name": "package", "subagents": ["executor"],
                                 "argv": ["git", "status", "--porcelain"],
                                 "outputs": ["dist/pkg.tgz"]}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            planned = plan_release_dry_run(steps, directory)
        self.assertEqual([p.name for p in planned], ["verify", "package"])
        self.assertEqual(planned[0].timeout, 30.0)
        self.assertEqual(planned[1].outputs, ("dist/pkg.tgz",))

    def test_dry_run_refuses_to_plan_a_mutating_verb(self) -> None:
        steps = [ToolStage.from_data({"name": "release", "subagents": ["executor"],
                                      "argv": ["git", "push", "origin", "main"]})]
        with self.assertRaisesRegex(SchemaError, "mutating verb"):
            plan_release_dry_run(steps, ".")


if __name__ == "__main__":
    unittest.main()
