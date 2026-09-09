"""REC-08 — restore operational required-input grants for a nested worker.

The fresh REC-06 launch (nested working root ``feature-pipeline-skill``; task, plan and prompt
under ``docs/``; required skills under the external ``.agents`` anchor) was blocked because the
production composition silently dropped every required-input directory: a skill declared with
the repository's logical ``.agents/`` prefix resolved through the in-workspace symlink and then
escaped the project anchor, so ``build_required_input_dirs`` discarded the whole task's grant
set instead of failing closed.

These tests drive the real production composition (``build_bootstrap`` ->
``make_execute_adapters`` -> ``adapter.plan``) for both the Claude and the Codex adapter and
prove the minimal ``docs`` and external-agents skill directories reach the actual argv, that an
unresolvable declared input fails closed before any child launch, and that none of it widens
the executor write scope or the verifier read-only posture.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from feature_pipeline.bootstrap import build_bootstrap, build_required_input_dirs
from feature_pipeline.contracts import TaskSpec
from pipeline_core.adapters import (
    REQUIRED_INPUT_INVALID,
    AdapterError,
    LaunchRequest,
    effective_grant,
)

_TDD_SKILL = ".agents/skills/software-development/test-driven-development/SKILL.md"
_TYPES_SKILL = (
    ".agents/skills/software-development/backend-dev/python/python-type-safety/SKILL.md"
)
_TDD_REL = "skills/software-development/test-driven-development"
_TYPES_REL = "skills/software-development/backend-dev/python/python-type-safety"


def _add_dirs(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv) if token == "--add-dir"]


class _Project:
    """A nested-route project: ``docs/`` inputs plus an *external* agents anchor."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.agents = Path(tempfile.mkdtemp())  # outside the project root, like the real tree
        (self.root / "docs" / "plans" / "tasks").mkdir(parents=True)
        (self.root / "feature-pipeline-skill").mkdir()
        (self.root / "core").mkdir()
        self.task_path = self.root / "docs" / "plans" / "tasks" / "REC-08_x.md"
        self.task_path.write_text("# REC-08\n", encoding="utf-8")
        self.plan_path = self.root / "docs" / "plans" / "2026-09-09-rec05.md"
        self.plan_path.write_text("# plan\n", encoding="utf-8")
        self.prompt_path = self.root / "docs" / "plans" / "prompt.md"
        self.prompt_path.write_text("prompt\n", encoding="utf-8")
        for rel in (f"{_TDD_REL}/SKILL.md", f"{_TYPES_REL}/SKILL.md"):
            skill = self.agents / rel
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text("skill\n", encoding="utf-8")

    def spec(self, **overrides: object) -> TaskSpec:
        base: dict[str, object] = dict(
            id="REC-08",
            task_type="tooling",
            executor="tooling-executor",
            allowed_scope=["feature-pipeline-skill/pipeline_core/adapters.py"],
            acceptance_criteria=["done"],
            path="docs/plans/tasks/REC-08_x.md",
            required_skills=[_TDD_SKILL, _TYPES_SKILL],
        )
        base.update(overrides)
        return TaskSpec.build(**base)  # type: ignore[arg-type]

    def grants(
        self, *, spec: TaskSpec | None = None, working_root: str = "feature-pipeline-skill"
    ) -> dict[str, tuple[str, ...]]:
        return build_required_input_dirs(
            [spec or self.spec()],
            project_dir=self.root,
            agents_root=self.agents,
            plan_path=self.plan_path,
            prompt_path=self.prompt_path,
            working_root_by_id={"REC-08": working_root},
        )

    def docs_dir(self) -> str:
        return str((self.root / "docs" / "plans").resolve())

    def tdd_dir(self) -> str:
        return str((self.agents / _TDD_REL).resolve())

    def types_dir(self) -> str:
        return str((self.agents / _TYPES_REL).resolve())


def _request(role: str = "tooling-executor", **overrides: object) -> LaunchRequest:
    base: dict[str, object] = dict(
        role=role,
        task_id="REC-08",
        prompt="EXECUTE THE TASK",
        report_path=Path("report.md"),
        working_root="feature-pipeline-skill",
        role_grant=("read", "write", "run_checks"),
        allowed_scope=("feature-pipeline-skill/pipeline_core/adapters.py",),
        tools=("Read", "Edit", "Bash"),
    )
    base.update(overrides)
    return LaunchRequest(**base)  # type: ignore[arg-type]


# --- focused regression: the derivation itself -------------------------------------------


class RequiredInputDerivationTests(unittest.TestCase):
    def test_dot_agents_prefixed_skill_resolves_to_the_external_agents_anchor(self) -> None:
        project = _Project()

        grants = project.grants()

        self.assertIn("REC-08", grants)
        dirs = set(grants["REC-08"])
        self.assertIn(project.docs_dir(), dirs)
        self.assertIn(project.tdd_dir(), dirs)
        self.assertIn(project.types_dir(), dirs)

    def test_a_project_root_worker_only_needs_the_external_skill_dirs(self) -> None:
        project = _Project()

        grants = project.grants(working_root=".")

        self.assertEqual(
            set(grants["REC-08"]), {project.tdd_dir(), project.types_dir()}
        )

    def test_a_bare_anchor_root_plan_at_the_project_root_needs_no_grant(self) -> None:
        # Regression: a pre-existing execute flow whose plan artifact is a bare ``plan.json``
        # beside the project root must not fail closed — the project-root worker already
        # reaches it, so it contributes no directory grant.
        project = _Project()
        plan = project.root / "plan.json"
        plan.write_text("{}\n", encoding="utf-8")
        spec = project.spec(required_skills=[])

        grants = build_required_input_dirs(
            [spec],
            project_dir=project.root,
            agents_root=project.agents,
            plan_path=plan,
            prompt_path=plan,
            working_root_by_id={"REC-08": "."},
        )

        self.assertEqual(grants, {})

    def test_a_bare_anchor_root_plan_a_nested_worker_cannot_reach_contributes_no_grant(
        self,
    ) -> None:
        # Regression: a bare ``plan.json`` beside the project root names no sub-anchor
        # directory to grant, and widening the grant to the whole project anchor is never
        # acceptable. Its content reaches the worker through the runner-composed context
        # bundle, so a nested worker that cannot reach it must not abort a pre-existing
        # execute flow — it simply contributes no directory grant.
        project = _Project()
        plan = project.root / "plan.json"
        plan.write_text("{}\n", encoding="utf-8")
        task_file = project.root / "feature-pipeline-skill" / "REC-08_x.md"
        task_file.write_text("# REC-08\n", encoding="utf-8")
        spec = project.spec(
            required_skills=[], path="feature-pipeline-skill/REC-08_x.md"
        )

        grants = build_required_input_dirs(
            [spec],
            project_dir=project.root,
            agents_root=project.agents,
            plan_path=plan,
            prompt_path=plan,
            working_root_by_id={"REC-08": "feature-pipeline-skill"},
        )

        self.assertEqual(grants, {})

    def test_a_missing_bare_anchor_root_input_fails_closed(self) -> None:
        project = _Project()
        missing = project.root / "plan.json"  # never created
        spec = project.spec(required_skills=[])

        with self.assertRaises(AdapterError) as caught:
            build_required_input_dirs(
                [spec],
                project_dir=project.root,
                agents_root=project.agents,
                plan_path=missing,
                prompt_path=missing,
                working_root_by_id={"REC-08": "feature-pipeline-skill"},
            )

        self.assertEqual(caught.exception.code, REQUIRED_INPUT_INVALID)

    def test_an_unresolvable_dot_agents_skill_fails_closed(self) -> None:
        project = _Project()
        spec = project.spec(required_skills=[".agents/skills/does/not/exist/SKILL.md"])

        with self.assertRaises(AdapterError) as caught:
            project.grants(spec=spec)

        self.assertEqual(caught.exception.code, REQUIRED_INPUT_INVALID)

    def test_an_unresolvable_plain_skill_fails_closed(self) -> None:
        project = _Project()
        spec = project.spec(required_skills=["skills/software-development/missing/SKILL.md"])

        with self.assertRaises(AdapterError) as caught:
            project.grants(spec=spec)

        self.assertEqual(caught.exception.code, REQUIRED_INPUT_INVALID)


# --- production composition: the grants reach the real adapter argv ---------------------


class ProductionCompositionTests(unittest.TestCase):
    def _composed(self, project: _Project, adapter: str):
        composition = build_bootstrap(
            project.root,
            project.agents,
            project.root / "core",
            required_input_dirs=project.grants(),
        )
        executor, _launchers, _environment = composition.make_execute_adapters(adapter)
        return executor

    def test_claude_nested_executor_argv_carries_docs_and_external_skill_dirs(self) -> None:
        project = _Project()

        argv = self._composed(project, "claude").plan(_request())

        add_dirs = _add_dirs(argv)
        self.assertIn(project.docs_dir(), add_dirs)
        self.assertIn(project.tdd_dir(), add_dirs)
        self.assertIn(project.types_dir(), add_dirs)
        # The write posture is untouched by a mandatory read input.
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Edit,Bash")

    def test_codex_nested_executor_argv_carries_docs_and_external_skill_dirs(self) -> None:
        project = _Project()

        argv = self._composed(project, "codex").plan(_request())

        add_dirs = _add_dirs(argv)
        self.assertIn(project.docs_dir(), add_dirs)
        self.assertIn(project.tdd_dir(), add_dirs)
        self.assertIn(project.types_dir(), add_dirs)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")

    def test_read_only_verifier_gets_the_read_inputs_without_write_expansion(self) -> None:
        project = _Project()
        request = _request(
            role="task_verifier",
            read_only=True,
            role_grant=("read",),
            tools=("Read",),
        )

        argv = self._composed(project, "claude").plan(request)

        add_dirs = _add_dirs(argv)
        self.assertIn(project.docs_dir(), add_dirs)
        self.assertIn(project.tdd_dir(), add_dirs)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "manual")
        disallowed = argv[argv.index("--disallowed-tools") + 1]
        for writer in ("Edit", "Write", "NotebookEdit"):
            self.assertIn(writer, disallowed)
        self.assertIn("Bash(git push:*)", disallowed)
        self.assertEqual(effective_grant(request), ("read",))

    def test_a_different_task_id_receives_none_of_the_injected_grants(self) -> None:
        project = _Project()

        argv = self._composed(project, "claude").plan(_request(task_id="OTHER"))

        self.assertNotIn("--add-dir", argv)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
