"""Tests for the stage 10–13 post-task lifecycle (documentation, audit, Graphify)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline_core.integrations import GraphifyIntegration
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.post_task import (
    DOCUMENTATION,
    DOCUMENTATION_AUDIT,
    GRAPHIFY_REFRESH,
    GRAPHIFY_VERIFICATION,
    POST_TASK_STAGES,
    PostTaskError,
    PostTaskRequest,
    run_post_task_lifecycle,
)
from pipeline_core.state import ACTOR_EXECUTOR, ACTOR_RUNNER, Run

_INTEGRATION = GraphifyIntegration.from_data({
    "schema_version": 1,
    "graphify": {
        "stage": "graphify",
        "executor": "runner:graphify",
        "workspace": "tools/graphify",
        "scan_root": ".",
        "wrapper_dir": "tools/graphify/scripts",
        "expected_outputs": [
            "tools/graphify/graphify-out/graph.json",
            "tools/graphify/graphify-out/manifest.json",
        ],
        "diff_policy": "tracked-empty",
        "forbidden": ["graphify claude install"],
    },
})


class _Spy:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        return dict(self.result)


def _outputs_runner(root: Path, *, create: bool = True, exit_code: int = 0):
    def runner(run, stage, cwd, argv, timeout=None):
        if create:
            for rel in _INTEGRATION.expected_outputs:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
        return {"exit_code": exit_code}
    return runner


def _life(root: Path, *, verified: bool = True, tasks=(("A-1", ()),)) -> RunLifecycle:
    prompt = root / "prompt.md"
    prompt.write_text("feature", encoding="utf-8")
    run = Run.create("docs", prompt, None, root / "runs" / "docs", root)
    life = RunLifecycle.initialize(run, tasks=[(tid, list(deps)) for tid, deps in tasks])
    wrapper = root / "tools" / "graphify" / "scripts" / "build.sh"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    if verified:
        for tid, _ in tasks:
            life.transition(tid, "running", actor=ACTOR_RUNNER)
            life.transition(tid, "implemented", actor=ACTOR_EXECUTOR)
            life.transition(tid, "verified", actor=ACTOR_RUNNER)
    return life


def _request(root: Path, life: RunLifecycle, **overrides) -> PostTaskRequest:
    kwargs = dict(
        selected_task_ids=tuple(life.run.tasks),
        integration=_INTEGRATION,
        root=root,
        documenter=_Spy({"status": "implemented", "report": "docs/x.md"}),
        auditor=_Spy({"verdict": "PASS"}),
        documentation_impact=("README.md",),
        command_runner=_outputs_runner(root),
        tracked_changes=lambda: [],
    )
    kwargs.update(overrides)
    return PostTaskRequest(**kwargs)


class LifecycleModelTests(unittest.TestCase):
    def test_stage_order_is_explicit(self) -> None:
        self.assertEqual(
            POST_TASK_STAGES,
            ((10, DOCUMENTATION), (11, DOCUMENTATION_AUDIT),
             (12, GRAPHIFY_REFRESH), (13, GRAPHIFY_VERIFICATION)),
        )

    def test_happy_path_runs_all_four_stages_and_persists_each_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            outcome = run_post_task_lifecycle(life, _request(root, life))

            self.assertEqual(outcome.status, "complete")
            self.assertEqual([s.verdict for s in outcome.stages], ["PASS"] * 4)

            reloaded = Run.load(life.run.run_dir, root)
            for number, name in POST_TASK_STAGES:
                self.assertEqual(reloaded.stages[name]["verdict"], "PASS", name)
                self.assertEqual(reloaded.stages[name]["number"], number)


class DocumentationGateTests(unittest.TestCase):
    def test_documentation_cannot_begin_while_a_selected_task_is_unverified(self) -> None:
        # AC-1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root, verified=False)
            request = _request(root, life)
            outcome = run_post_task_lifecycle(life, request)

            self.assertEqual(outcome.status, "gate-pending")
            self.assertEqual(outcome.stage(DOCUMENTATION).verdict, "BLOCKED")
            self.assertEqual(request.documenter.calls, 0)
            self.assertIsNone(outcome.stage(DOCUMENTATION_AUDIT))
            self.assertEqual(
                Run.load(life.run.run_dir, root).stages[DOCUMENTATION]["verdict"], "BLOCKED")

    def test_a_failed_documentation_context_blocks_before_the_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            request = _request(
                root, life, documenter=_Spy({"status": "blocked", "failure": "no write path"}))
            outcome = run_post_task_lifecycle(life, request)

            self.assertEqual(outcome.status, "blocked")
            self.assertEqual(outcome.stage(DOCUMENTATION).verdict, "BLOCKED")
            self.assertIsNone(outcome.stage(DOCUMENTATION_AUDIT))


class DocumentationAuditGateTests(unittest.TestCase):
    def test_graphify_cannot_begin_without_a_passing_independent_audit(self) -> None:
        # AC-2
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            graphify_runner = _outputs_runner(root)
            calls = []
            request = _request(
                root, life,
                auditor=_Spy({"verdict": "FAIL"}),
                command_runner=lambda *a, **k: calls.append(a) or graphify_runner(*a, **k),
            )
            outcome = run_post_task_lifecycle(life, request)

            self.assertEqual(outcome.status, "blocked")
            self.assertEqual(outcome.stage(DOCUMENTATION_AUDIT).verdict, "FAIL")
            self.assertIsNone(outcome.stage(GRAPHIFY_REFRESH))
            self.assertEqual(calls, [])

    def test_audit_must_run_in_a_context_separate_from_documentation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            shared = _Spy({"status": "implemented", "verdict": "PASS", "report": "d.md"})
            request = _request(root, life, documenter=shared, auditor=shared)
            with self.assertRaises(PostTaskError):
                run_post_task_lifecycle(life, request)

    def test_an_unknown_audit_verdict_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            request = _request(root, life, auditor=_Spy({"verdict": "MAYBE"}))
            with self.assertRaises(PostTaskError):
                run_post_task_lifecycle(life, request)


class GraphifyStageTests(unittest.TestCase):
    def test_a_forbidden_installer_argv_is_refused_before_it_runs(self) -> None:
        # AC-3
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            calls = []
            request = _request(
                root, life,
                graphify_argv=("graphify", "claude", "install"),
                command_runner=lambda *a, **k: calls.append(a) or {"exit_code": 0},
            )
            outcome = run_post_task_lifecycle(life, request)

            self.assertEqual(outcome.status, "blocked")
            stage = outcome.stage(GRAPHIFY_REFRESH)
            self.assertEqual(stage.verdict, "BLOCKED")
            self.assertIn("installer", stage.reasons[0])
            self.assertEqual(calls, [])
            self.assertIsNone(outcome.stage(GRAPHIFY_VERIFICATION))

    def test_a_mutating_git_verb_in_the_argv_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            calls = []
            request = _request(
                root, life,
                graphify_argv=("git", "push", "origin", "main"),
                command_runner=lambda *a, **k: calls.append(a) or {"exit_code": 0},
            )
            outcome = run_post_task_lifecycle(life, request)
            self.assertEqual(outcome.stage(GRAPHIFY_REFRESH).verdict, "BLOCKED")
            self.assertEqual(calls, [])

    def test_an_absent_wrapper_blocks_the_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            (root / "tools" / "graphify" / "scripts" / "build.sh").unlink()
            outcome = run_post_task_lifecycle(life, _request(root, life))
            self.assertEqual(outcome.status, "blocked")
            self.assertEqual(outcome.stage(GRAPHIFY_REFRESH).verdict, "BLOCKED")

    def test_a_missing_configured_output_fails_the_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            request = _request(
                root, life, command_runner=_outputs_runner(root, create=False))
            outcome = run_post_task_lifecycle(life, request)
            self.assertEqual(outcome.status, "blocked")
            self.assertEqual(outcome.stage(GRAPHIFY_REFRESH).verdict, "FAIL")
            self.assertTrue(outcome.stage(GRAPHIFY_REFRESH).evidence["missing_outputs"])

    def test_stage_12_catches_a_newly_introduced_tracked_change(self) -> None:
        # AC-3: a tracked diff produced by the refresh.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            changes: list[str] = []
            base_runner = _outputs_runner(root)

            def runner(*a, **k):
                changes.append("M tools/graphify/graphify-out/graph.json")
                return base_runner(*a, **k)

            request = _request(
                root, life, command_runner=runner, tracked_changes=lambda: list(changes))
            outcome = run_post_task_lifecycle(life, request)
            self.assertEqual(outcome.status, "blocked")
            self.assertEqual(outcome.stage(GRAPHIFY_REFRESH).verdict, "FAIL")

    def test_stage_13_independently_rejects_a_pre_existing_tracked_diff(self) -> None:
        # AC-3: stage 12's before/after delta is clean, but a tracked change under the
        # Graphify workspace is still present — stage 13 must catch it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            dirty = ["M tools/graphify/graphify-out/graph.json"]
            request = _request(
                root, life,
                command_runner=_outputs_runner(root),
                tracked_changes=lambda: list(dirty))
            outcome = run_post_task_lifecycle(life, request)
            self.assertEqual(outcome.status, "blocked")
            self.assertEqual(outcome.stage(GRAPHIFY_REFRESH).verdict, "PASS")
            self.assertEqual(outcome.stage(GRAPHIFY_VERIFICATION).verdict, "FAIL")

    def test_stage_13_ignores_a_tracked_diff_outside_the_graphify_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            request = _request(
                root, life,
                command_runner=_outputs_runner(root),
                tracked_changes=lambda: ["M docs/unrelated.md"])
            outcome = run_post_task_lifecycle(life, request)
            self.assertEqual(outcome.status, "complete")


class RestartSafetyTests(unittest.TestCase):
    def test_a_passed_stage_is_not_rerun_after_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _life(root)
            documenter = _Spy({"status": "implemented", "report": "docs/x.md"})
            first = _request(
                root, life, documenter=documenter, auditor=_Spy({"verdict": "FAIL"}))
            self.assertEqual(run_post_task_lifecycle(life, first).status, "blocked")
            self.assertEqual(documenter.calls, 1)

            reloaded = RunLifecycle.load(life.run.run_dir, root)
            second = _request(
                root, reloaded, documenter=documenter, auditor=_Spy({"verdict": "PASS"}))
            outcome = run_post_task_lifecycle(reloaded, second)

            self.assertEqual(outcome.status, "complete")
            self.assertEqual(documenter.calls, 1)  # stage 10 was already PASS on run.json


if __name__ == "__main__":
    unittest.main()
