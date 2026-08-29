"""Tests for the portable core runner CLI (`scripts/run_pipeline.py`).

The CLI logic lives in :mod:`pipeline_core.runner_cli`; ``scripts/run_pipeline.py`` is a
thin shim over :func:`pipeline_core.runner_cli.main`. These tests drive ``main`` directly
with an explicit ``argv`` so nothing depends on ``sys.argv`` or process exit.
"""

from __future__ import annotations

import io
import json
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline_core import runner_cli

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# Identifiers that must never leak into the portable core or its --help output.
PROJECT_IDENTITY_LITERALS = ("graphify", "cargo", "npm", "oxidium")

MINIMAL_REGISTRY = {
    "task_types": {
        "docs": {
            "stack": "text",
            "subagents": ["executor"],
            "root": "content",
            "checks": ["lint"],
            "storage": "runs",
        }
    },
    "stacks": {"text": {"runtime": "text"}},
    "subagents": {"executor": {"grant": "executor"}},
    "roots": {"content": "content"},
    "checks": {"lint": ["lint", "run"]},
    "storage": {"runs": ".pipeline/runs"},
}


def _run(args: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = runner_cli.main(args)
    return code, out.getvalue(), err.getvalue()


def _seed(fixture: str, dest: Path, *, tasks: list[dict], with_registry: bool = True) -> dict:
    """Copy a portable fixture into ``dest`` and add a plan; return anchor + arg data."""
    shutil.copytree(FIXTURES / fixture, dest, dirs_exist_ok=True)
    if fixture == "library-guide":
        profile_rel, project_subdir = "profile.json", "."
    else:
        profile_rel, project_subdir = "config/pipeline.json", "workspace"
    profile_path = dest / profile_rel
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if with_registry:
        profile["registry"] = json.loads(json.dumps(MINIMAL_REGISTRY))
    profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")

    project_dir = dest if project_subdir == "." else dest / project_subdir
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "plan.json").write_text(
        json.dumps({"feature": "sample-feature", "tasks": tasks}, indent=2) + "\n",
        encoding="utf-8",
    )
    agents_root = dest / "_agents"
    (agents_root / "skills").mkdir(parents=True, exist_ok=True)
    return {
        "anchors": [
            "--project-root", str(dest),
            "--agents-root", str(agents_root),
            "--core-root", str(dest),
        ],
        "profile_rel": profile_rel,
        "project_dir": project_dir,
        "dest": dest,
    }


class HelpAndFlagSurfaceTests(unittest.TestCase):
    def test_help_lists_every_anchor_and_core_operational_flag(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            _run(["--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_help_text_carries_no_project_identity_literal(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            runner_cli.main(["--help"])
        help_text = out.getvalue().lower()
        for flag in (
            "--project-root", "--agents-root", "--core-root", "--profile", "--project-skill",
            "--plan", "--task", "--through", "--resume", "--mode", "--approve-plan",
            "--dry-run", "--commit", "--push",
        ):
            self.assertIn(flag, help_text)
        for literal in PROJECT_IDENTITY_LITERALS:
            self.assertNotIn(literal, help_text)

    def test_cli_source_carries_no_project_identity_literal(self) -> None:
        root = Path(runner_cli.__file__).resolve().parents[1]
        sources = [
            root / "pipeline_core" / "runner_cli.py",
            root / "scripts" / "run_pipeline.py",
        ]
        for source in sources:
            text = source.read_text(encoding="utf-8").lower()
            for literal in PROJECT_IDENTITY_LITERALS:
                self.assertNotIn(literal, text, f"{source.name} contains '{literal}'")


class PushDenialTests(unittest.TestCase):
    def test_push_is_refused_before_any_artifact_with_the_baseline_message(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            code, _, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--dry-run", "--push",
            ])
            self.assertEqual(code, 1)
            self.assertEqual(
                err.strip(),
                "push is denied at this stage and the runner has no push code path.",
            )
            self.assertFalse(list(seed["project_dir"].glob("**/run.json")))

    def test_push_is_refused_even_without_other_arguments(self) -> None:
        code, _, err = _run(["--push"])
        self.assertEqual(code, 1)
        self.assertIn("push is denied at this stage", err)


class RelativeAnchorRejectionTests(unittest.TestCase):
    def _reject(self, profile_value: str) -> tuple[int, str]:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            code, _, err = _run(seed["anchors"] + [
                "--profile", profile_value, "--plan", "plan.json", "--dry-run",
            ])
        return code, err

    def test_absolute_profile_path_is_rejected(self) -> None:
        code, err = self._reject("/etc/passwd")
        self.assertEqual(code, 30)
        self.assertIn("relative", err.lower())

    def test_home_anchored_profile_path_is_rejected(self) -> None:
        code, err = self._reject("~/profile.json")
        self.assertEqual(code, 30)

    def test_parent_traversal_profile_path_is_rejected(self) -> None:
        code, err = self._reject("../profile.json")
        self.assertEqual(code, 30)
        self.assertIn("traverse", err.lower())

    def test_backslash_separator_profile_path_is_rejected(self) -> None:
        code, err = self._reject("nested\\profile.json")
        self.assertEqual(code, 30)

    def test_rejection_message_does_not_leak_an_absolute_host_path(self) -> None:
        code, err = self._reject("/etc/passwd")
        self.assertEqual(code, 30)
        self.assertNotIn("/etc/passwd", err)


C1_TO_C8 = ("C1.", "C2.", "C3.", "C4.", "C5.", "C6.", "C7.", "C8.")


class DryRunPlanShapeTests(unittest.TestCase):
    def test_dry_run_against_library_guide_prints_full_shape_and_is_byte_identical(self) -> None:
        tasks = [
            {"id": "T-01", "type": "docs"},
            {"id": "T-02", "type": "docs", "depends_on": ["T-01"]},
        ]
        outputs = []
        for _ in range(2):
            with TemporaryDirectory() as directory:
                dest = Path(directory) / "project"
                seed = _seed("library-guide", dest, tasks=tasks)
                code, out, err = _run(seed["anchors"] + [
                    "--profile", seed["profile_rel"], "--project-skill", "skills",
                    "--plan", "plan.json", "--dry-run",
                ])
                self.assertEqual(code, 0, err)
                self.assertNotIn(str(dest), out)
                self.assertNotIn(directory, out)
                outputs.append(out)
        for marker in C1_TO_C8:
            self.assertIn(marker, outputs[0])
        self.assertIn("T-01", outputs[0])
        self.assertIn("T-02", outputs[0])
        self.assertIn("stack=text", outputs[0])
        self.assertEqual(outputs[0], outputs[1], "dry-run output is not deterministic")

    def test_dry_run_against_incident_notes_also_prints_full_shape(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("incident-notes", dest, tasks=[{"id": "N-01", "type": "docs"}])
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ])
        self.assertEqual(code, 0, err)
        for marker in C1_TO_C8:
            self.assertIn(marker, out)
        self.assertIn("notes update", out)  # a stage argv from the incident-notes profile

    def test_dry_run_writes_nothing_into_the_project_tree(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            before = {p for p in dest.rglob("*")}
            code, _, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ])
            after = {p for p in dest.rglob("*")}
        self.assertEqual(code, 0, err)
        self.assertEqual(before, after)


class FailClosedRoutingTests(unittest.TestCase):
    def test_design_task_type_fails_closed_with_unresolved_executor(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "D-01", "type": "design"}])
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("unresolved-executor", out)

    def test_unknown_task_type_fails_closed_with_unknown_task_type(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "X-01", "type": "sculpture"}])
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("unknown-task-type", out)

    def test_profile_without_a_registry_fails_closed_for_every_task(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}],
                         with_registry=False)
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("unresolved-executor", out)


class DependencyAndCommitGateTests(unittest.TestCase):
    def test_task_with_an_unmet_dependency_fails_closed(self) -> None:
        tasks = [
            {"id": "T-01", "type": "docs"},
            {"id": "T-02", "type": "docs", "depends_on": ["T-01"]},
        ]
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=tasks)
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--task", "T-02", "--dry-run",
            ])
        self.assertEqual(code, 20, err)
        self.assertIn("dependency-not-satisfied", out)
        self.assertIn("T-01", out)

    def test_commit_flag_cannot_skip_the_plan_gate_in_a_real_run(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json", "--commit",
            ])
        self.assertEqual(code, 10, err)
        self.assertIn("plan gate pending", out)

    def test_commit_gate_stays_pending_in_the_dry_run_plan(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            seed = _seed("library-guide", dest, tasks=[{"id": "T-01", "type": "docs"}])
            code, out, err = _run(seed["anchors"] + [
                "--profile", seed["profile_rel"], "--plan", "plan.json",
                "--dry-run", "--commit",
            ])
        self.assertEqual(code, 0, err)
        self.assertIn("commit - pending", out)


if __name__ == "__main__":
    unittest.main()
