"""REC-05 — mandatory input access for nested adapter workers.

A worker launched into a nested working root (``feature-pipeline-skill``) whose task and plan
files live under ``docs/`` and whose required skill lives under the external ``.agents`` anchor
must receive shell-free, minimal native ``--add-dir`` grants for exactly those inputs — for
both the Claude and the Codex adapters, for executors and for fresh read-only verifiers alike,
and without any of it widening ``allowed_scope`` or a write capability.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from feature_pipeline.bootstrap import build_required_input_dirs
from feature_pipeline.contracts import TaskSpec
from pipeline_core.adapters import (
    REQUIRED_INPUT_INVALID,
    AdapterError,
    ClaudeAdapter,
    CodexAdapter,
    CompletedProcess,
    LaunchRequest,
    RequiredInput,
    build_claude_argv,
    build_codex_argv,
    derive_required_input_dirs,
    effective_grant,
)


def _request(role: str = "python-executor", **overrides: object) -> LaunchRequest:
    base: dict[str, object] = dict(
        role=role,
        task_id="REC-05",
        prompt="EXECUTE THE TASK",
        report_path=Path("report.md"),
        working_root="feature-pipeline-skill",
        role_grant=("read", "write", "run_checks"),
        allowed_scope=("feature-pipeline-skill/pipeline_core/adapters.py",),
        tools=("Read", "Edit", "Bash"),
    )
    base.update(overrides)
    return LaunchRequest(**base)  # type: ignore[arg-type]


def _add_dirs(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, value in enumerate(argv) if value == "--add-dir"]


class _CapturingRunner:
    def __init__(self) -> None:
        self.argv: list[str] | None = None

    def __call__(self, argv, *, prompt="", cwd=None, timeout=None, env=None):
        self.argv = list(argv)
        return CompletedProcess(0, '{"result": "implemented", "session_id": "s-1"}', "")


# --- RED: minimal native argv grants for the declared inputs -------------------------------


class RequiredInputArgvTests(unittest.TestCase):
    DIRS = ("/proj/docs/plans", "/proj/.prompts", "/agents/skills/tdd")

    def test_claude_executor_argv_carries_exactly_the_required_input_dirs(self) -> None:
        argv = build_claude_argv(
            _request(required_input_dirs=self.DIRS), executable="claude"
        )
        self.assertEqual(_add_dirs(argv), list(self.DIRS))
        # The read/write posture is untouched by a mandatory read input.
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Edit,Bash")
        # The mandatory-read-input grants themselves carry no shell composition. The check is
        # scoped to the ``--add-dir`` values on purpose: an unrelated argv token such as the
        # always-present ``Bash(git push:*)`` deny rule legitimately contains a parenthesis.
        for token in _add_dirs(argv):
            self.assertFalse(any(ch in token for ch in "|&;<>`\n\r$"), token)

    def test_codex_executor_argv_appends_required_input_dirs_after_scoped_write_roots(self) -> None:
        argv = build_codex_argv(
            _request(required_input_dirs=self.DIRS),
            executable="codex",
            working_root="/proj/feature-pipeline-skill",
            add_dirs=("/agents/skills/example",),
        )
        self.assertEqual(
            _add_dirs(argv), ["/agents/skills/example", *self.DIRS]
        )
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")

    def test_duplicate_between_scoped_and_required_dirs_is_emitted_once(self) -> None:
        argv = build_claude_argv(
            _request(required_input_dirs=("/agents/skills/tdd", "/proj/docs/plans")),
            executable="claude",
            add_dirs=("/agents/skills/tdd",),
        )
        self.assertEqual(_add_dirs(argv), ["/agents/skills/tdd", "/proj/docs/plans"])

    def test_shell_metacharacter_in_a_required_dir_is_refused(self) -> None:
        with self.assertRaises(AdapterError) as caught:
            build_claude_argv(
                _request(required_input_dirs=("/proj/docs`whoami`",)), executable="claude"
            )
        self.assertEqual(caught.exception.code, "shell-metacharacter")


# --- RED: verifier launches get read inputs, keep read-only posture -----------------------


class VerifierRequiredInputTests(unittest.TestCase):
    DIRS = ("/proj/docs/plans", "/agents/skills/tdd")

    def test_read_only_verifier_gets_the_read_inputs_without_any_write_expansion(self) -> None:
        request = _request(
            role="task_verifier",
            read_only=True,
            role_grant=("read",),
            tools=("Read",),
            required_input_dirs=self.DIRS,
        )
        argv = build_claude_argv(request, executable="claude")
        self.assertEqual(_add_dirs(argv), list(self.DIRS))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "manual")
        disallowed = argv[argv.index("--disallowed-tools") + 1]
        for writer in ("Edit", "Write", "NotebookEdit"):
            self.assertIn(writer, disallowed)
        self.assertIn("Bash(git push:*)", disallowed)
        # A mandatory read input never becomes a capability grant.
        self.assertEqual(effective_grant(request), ("read",))

    def test_codex_read_only_verifier_keeps_its_read_only_sandbox_with_the_grants(self) -> None:
        argv = build_codex_argv(
            _request(
                role="task_verifier", read_only=True, role_grant=("read",),
                required_input_dirs=self.DIRS,
            ),
            executable="codex",
            working_root="/proj/feature-pipeline-skill",
        )
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(_add_dirs(argv), list(self.DIRS))


# --- RED: adapter injection keyed by task id ----------------------------------------------


class AdapterInjectionTests(unittest.TestCase):
    def test_claude_adapter_injects_the_matching_task_grants_on_a_fresh_launch(self) -> None:
        runner = _CapturingRunner()
        adapter = ClaudeAdapter(
            executable="claude",
            runner=runner,
            required_input_dirs={"REC-05": ("/proj/docs/plans", "/agents/skills/tdd")},
        )
        adapter.launch(_request(required_input_dirs=()))
        assert runner.argv is not None
        self.assertEqual(
            _add_dirs(runner.argv), ["/proj/docs/plans", "/agents/skills/tdd"]
        )

    def test_claude_adapter_preserves_runtime_repair_grants_when_injecting_task_inputs(self) -> None:
        runner = _CapturingRunner()
        adapter = ClaudeAdapter(
            executable="claude",
            runner=runner,
            required_input_dirs={"REC-05": ("/proj/docs/plans",)},
        )

        adapter.launch(_request(required_input_dirs=("/workspace/.pipeline/reports/REC-05",)))

        assert runner.argv is not None
        self.assertEqual(
            _add_dirs(runner.argv),
            ["/workspace/.pipeline/reports/REC-05", "/proj/docs/plans"],
        )

    def test_a_different_task_id_receives_no_injected_grants(self) -> None:
        runner = _CapturingRunner()
        adapter = ClaudeAdapter(
            executable="claude",
            runner=runner,
            required_input_dirs={"OTHER": ("/proj/docs/plans",)},
        )
        adapter.launch(_request(task_id="REC-05", required_input_dirs=()))
        assert runner.argv is not None
        self.assertNotIn("--add-dir", runner.argv)

    def test_a_resumed_continuation_is_not_re_granted(self) -> None:
        runner = _CapturingRunner()
        adapter = ClaudeAdapter(
            executable="claude",
            runner=runner,
            required_input_dirs={"REC-05": ("/proj/docs/plans",)},
        )
        adapter.launch(
            _request(required_input_dirs=(), resume_session_id="s-1", fresh_session=False)
        )
        assert runner.argv is not None
        self.assertNotIn("--add-dir", runner.argv)

    def test_codex_adapter_plan_injects_the_matching_task_grants(self) -> None:
        adapter = CodexAdapter(
            executable="codex",
            working_root="/proj",
            required_input_dirs={"REC-05": ("/proj/docs/plans",)},
        )
        argv = adapter.plan(_request(working_root="feature-pipeline-skill"))
        self.assertEqual(
            _add_dirs(argv),
            [str(Path("/proj") / "feature-pipeline-skill"), "/proj/docs/plans"],
        )


# --- RED: fail-closed validation ---------------------------------------------------------


class RequiredInputDenialTests(unittest.TestCase):
    def _project(self) -> tuple[Path, Path]:
        directory = tempfile.mkdtemp()
        root = Path(directory)
        (root / "docs").mkdir()
        agents = root / "agents"
        agents.mkdir()
        return root, agents

    def test_unsafe_logical_sources_are_rejected(self) -> None:
        for bad in ("../escape.md", "/abs/x.md", "C:/Users/x.md", r"a\b.md",
                    "a/../b.md", "a/./b.md", "  x.md", "", "trailing/"):
            with self.subTest(source=bad):
                with self.assertRaises(AdapterError) as caught:
                    RequiredInput("task", "project", bad).validate()
                self.assertEqual(caught.exception.code, REQUIRED_INPUT_INVALID)

    def test_a_bare_anchor_root_file_names_no_directory_to_grant(self) -> None:
        with self.assertRaises(AdapterError) as caught:
            RequiredInput("plan", "project", "PLAN.md").validate()
        self.assertEqual(caught.exception.code, REQUIRED_INPUT_INVALID)

    def test_unknown_kind_or_anchor_is_rejected(self) -> None:
        with self.assertRaises(AdapterError):
            RequiredInput("secret", "project", "docs/x.md").validate()
        with self.assertRaises(AdapterError):
            RequiredInput("task", "nowhere", "docs/x.md").validate()

    def test_a_missing_anchor_fails_closed(self) -> None:
        root, agents = self._project()
        with self.assertRaises(AdapterError) as caught:
            derive_required_input_dirs(
                [RequiredInput("task", "project", "docs/x.md")],
                project_root=root,
                agents_root=root / "does-not-exist",
            )
        self.assertEqual(caught.exception.code, REQUIRED_INPUT_INVALID)

    def test_the_same_source_under_two_anchors_is_ambiguous(self) -> None:
        root, agents = self._project()
        with self.assertRaises(AdapterError) as caught:
            derive_required_input_dirs(
                [
                    RequiredInput("skill", "project", "skills/tdd/SKILL.md"),
                    RequiredInput("skill", "agents", "skills/tdd/SKILL.md"),
                ],
                project_root=root,
                agents_root=agents,
            )
        self.assertEqual(caught.exception.code, REQUIRED_INPUT_INVALID)

    def test_a_traversal_that_escapes_the_anchor_fails_closed(self) -> None:
        root, agents = self._project()
        deep = root / "docs" / "plans"
        deep.mkdir()
        # A symlink-free logical source cannot escape once ``..`` is banned, so the
        # ``relative_to`` backstop is exercised through an already-normalised anchor.
        with self.assertRaises(AdapterError):
            derive_required_input_dirs(
                [RequiredInput("task", "project", "../outside/x.md")],
                project_root=deep,
                agents_root=agents,
            )


# --- GREEN target: minimal derivation + collapse -----------------------------------------


class DeriveRequiredInputDirsTests(unittest.TestCase):
    def _project(self) -> tuple[Path, Path]:
        root = Path(tempfile.mkdtemp())
        (root / "docs" / "plans" / "tasks").mkdir(parents=True)
        (root / ".prompts").mkdir()
        (root / "feature-pipeline-skill").mkdir()
        agents = Path(tempfile.mkdtemp())  # an external anchor, outside the project root
        (agents / "skills" / "tdd").mkdir(parents=True)
        return root, agents

    def _inputs(self) -> list[RequiredInput]:
        return [
            RequiredInput("task", "project", "docs/plans/tasks/REC-05_x.md"),
            RequiredInput("plan", "project", "docs/plans/2026-09-09-x.md"),
            RequiredInput("prompt", "project", ".prompts/feature.md"),
            RequiredInput("skill", "agents", "skills/tdd/SKILL.md"),
        ]

    def test_collapses_child_dirs_and_keeps_the_external_skill_root(self) -> None:
        root, agents = self._project()
        dirs = derive_required_input_dirs(
            self._inputs(),
            project_root=root,
            agents_root=agents,
            reachable_roots=(root / "feature-pipeline-skill",),
        )
        self.assertEqual(
            set(dirs),
            {
                str((root / "docs" / "plans").resolve()),
                str((root / ".prompts").resolve()),
                str((agents / "skills" / "tdd").resolve()),
            },
        )

    def test_dirs_already_reachable_from_the_working_root_are_dropped(self) -> None:
        root, agents = self._project()
        dirs = derive_required_input_dirs(
            self._inputs(), project_root=root, agents_root=agents,
            reachable_roots=(root,),
        )
        self.assertEqual(set(dirs), {str((agents / "skills" / "tdd").resolve())})


class BuildRequiredInputDirsTests(unittest.TestCase):
    def _spec(self, root: Path) -> TaskSpec:
        (root / "docs" / "plans" / "tasks").mkdir(parents=True)
        (root / "docs" / "plans" / "tasks" / "REC-05_x.md").write_text("# t\n", encoding="utf-8")
        (root / "docs" / "plans" / "plan.md").write_text("# p\n", encoding="utf-8")
        return TaskSpec.build(
            id="REC-05", task_type="python", executor="python-executor",
            allowed_scope=["feature-pipeline-skill/pipeline_core/adapters.py"],
            acceptance_criteria=["AC-1"], path="docs/plans/tasks/REC-05_x.md",
            required_skills=["skills/tdd/SKILL.md"],
        )

    def test_a_nested_task_gets_grants_a_project_root_task_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / "agents"
            (agents / "skills" / "tdd").mkdir(parents=True)
            (agents / "skills" / "tdd" / "SKILL.md").write_text("s\n", encoding="utf-8")
            spec = self._spec(root)
            plan = root / "docs" / "plans" / "plan.md"

            nested = build_required_input_dirs(
                [spec], project_dir=root, agents_root=agents, plan_path=plan,
                prompt_path=plan, working_root_by_id={"REC-05": "feature-pipeline-skill"},
            )
            flat = build_required_input_dirs(
                [spec], project_dir=root, agents_root=agents, plan_path=plan,
                prompt_path=plan, working_root_by_id={"REC-05": "."},
            )

        self.assertIn("REC-05", nested)
        self.assertIn(str((agents / "skills" / "tdd").resolve()), nested["REC-05"])
        self.assertIn(str((root / "docs" / "plans").resolve()), nested["REC-05"])
        self.assertEqual(flat, {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
