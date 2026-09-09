"""REC-01 AC-1 — the runner-owned immutable executor-context bundle.

A child launched into a nested working root (``feature-pipeline-skill``) must receive the exact
task / plan / prompt / required-skill content without reading those files from outside its
working root and without any widened write root. The bundle is that content: immutable, bound
to a safe project/agents-root logical source and a SHA-256 digest, and fail-closed on tamper.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from feature_pipeline.bootstrap import build_executor_context_bundles
from feature_pipeline.contracts import TaskSpec
from pipeline_core.adapters import (
    CONTEXT_BUNDLE_INVALID,
    AdapterError,
    ClaudeAdapter,
    CompletedProcess,
    ContextEntry,
    ExecutorContextBundle,
    LaunchRequest,
    build_claude_argv,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry(kind: str = "task", source: str = "docs/plans/tasks/REC-01.md",
           content: str = "task body") -> ContextEntry:
    return ContextEntry.of(kind, source, content)


def _bundle(task_id: str = "REC-01", *entries: ContextEntry) -> ExecutorContextBundle:
    return ExecutorContextBundle(task_id, entries or (_entry(),))


def _request(**overrides: object) -> LaunchRequest:
    base: dict[str, object] = dict(
        role="executor", task_id="REC-01", prompt="EXECUTE THE TASK",
        report_path=Path("report.md"), role_grant=("read", "write", "run_checks"),
        tools=("Read", "Edit", "Bash"), allowed_scope=("src/feature_pipeline/bootstrap.py",),
    )
    base.update(overrides)
    return LaunchRequest(**base)  # type: ignore[arg-type]


class _CapturingRunner:
    """A process runner stand-in that records the stdin prompt and never spawns anything."""

    def __init__(self) -> None:
        self.prompt: str | None = None

    def __call__(self, argv, *, prompt="", cwd=None, timeout=None, env=None):
        self.prompt = prompt
        return CompletedProcess(0, '{"result": "implemented", "session_id": "sess-1"}', "")


# --- digest + source safety -----------------------------------------------------------------


class ContextEntryValidationTests(unittest.TestCase):
    def test_of_binds_a_matching_sha256_digest(self) -> None:
        entry = ContextEntry.of("task", "docs/tasks/REC-01.md", "hello\nworld\n")
        self.assertEqual(entry.digest, _sha("hello\nworld\n"))
        entry.validate()

    def test_a_tampered_digest_fails_closed(self) -> None:
        entry = ContextEntry("task", "docs/tasks/REC-01.md", _sha("original"), "tampered")
        with self.assertRaises(AdapterError) as caught:
            entry.validate()
        self.assertEqual(caught.exception.code, CONTEXT_BUNDLE_INVALID)

    def test_unsafe_logical_sources_are_rejected(self) -> None:
        for bad in (
            "../escape.md", "/abs/host/path.md", "C:/Users/admin/x.md", r"a\b.md",
            "a/../b.md", "a/./b.md", "  leading.md", "", "trailing/",
        ):
            with self.subTest(source=bad):
                with self.assertRaises(AdapterError) as caught:
                    ContextEntry.of("task", bad, "body")
                self.assertEqual(caught.exception.code, CONTEXT_BUNDLE_INVALID)

    def test_dotfile_directory_sources_are_allowed(self) -> None:
        ContextEntry.of("skill", ".agents/skills/tdd/SKILL.md", "skill body").validate()

    def test_host_absolute_path_in_content_fails_closed(self) -> None:
        for leaky in (r"see C:\Users\admin\secret", "cd /home/admin/project"):
            with self.subTest(content=leaky):
                with self.assertRaises(AdapterError) as caught:
                    ContextEntry.of("plan", "docs/plan.md", leaky)
                self.assertEqual(caught.exception.code, CONTEXT_BUNDLE_INVALID)

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(AdapterError):
            ContextEntry.of("secrets", "docs/x.md", "body")


class ExecutorContextBundleValidationTests(unittest.TestCase):
    def test_a_bundle_without_a_task_contract_fails_closed(self) -> None:
        with self.assertRaises(AdapterError) as caught:
            _bundle("REC-01", _entry("plan", "docs/plan.md", "p")).validate()
        self.assertEqual(caught.exception.code, CONTEXT_BUNDLE_INVALID)

    def test_a_duplicate_kind_source_pair_fails_closed(self) -> None:
        dup = _entry("skill", ".agents/skills/tdd/SKILL.md", "a")
        with self.assertRaises(AdapterError):
            _bundle("REC-01", _entry(), dup, dup).validate()

    def test_render_carries_every_entry_verbatim_with_its_digest(self) -> None:
        task = _entry("task", "docs/tasks/REC-01.md", "TASK CONTRACT")
        plan = _entry("plan", "docs/plan.md", "PLAN CONTENT")
        rendered = ExecutorContextBundle("REC-01", (task, plan)).render()
        self.assertIn("TASK CONTRACT", rendered)
        self.assertIn("PLAN CONTENT", rendered)
        self.assertIn(f"sha256:{task.digest}", rendered)
        self.assertIn("Assigned task: REC-01", rendered)

    def test_render_fails_closed_on_a_tampered_entry(self) -> None:
        bad = ContextEntry("task", "docs/tasks/REC-01.md", _sha("x"), "y")
        with self.assertRaises(AdapterError):
            ExecutorContextBundle("REC-01", (bad,)).render()


# --- adapter delivery into the child envelope ----------------------------------------------


class ClaudeAdapterContextDeliveryTests(unittest.TestCase):
    def _adapter(self, runner, **kw):
        return ClaudeAdapter(executable="claude", runner=runner, **kw)

    def test_exact_task_plan_prompt_skill_content_reaches_the_child_prompt(self) -> None:
        bundle = ExecutorContextBundle("REC-01", (
            _entry("task", "docs/plans/tasks/REC-01.md", "CANONICAL TASK CONTRACT"),
            _entry("plan", "docs/plans/routing.md", "PLAN BODY FOR REC-01"),
            _entry("prompt", ".prompts/feature.md", "FEATURE PROMPT TEXT"),
            _entry("skill", ".agents/skills/tdd/SKILL.md", "TDD SKILL CONTENT"),
        ))
        runner = _CapturingRunner()
        adapter = self._adapter(runner, executor_contexts={"REC-01": bundle})
        adapter.launch(_request())
        assert runner.prompt is not None
        for needle in ("CANONICAL TASK CONTRACT", "PLAN BODY FOR REC-01",
                       "FEATURE PROMPT TEXT", "TDD SKILL CONTENT", "EXECUTE THE TASK"):
            self.assertIn(needle, runner.prompt)

    def test_a_task_with_no_bundle_launches_with_the_plain_prompt(self) -> None:
        runner = _CapturingRunner()
        self._adapter(runner, executor_contexts={"OTHER": _bundle("OTHER")}).launch(_request())
        self.assertEqual(runner.prompt, "EXECUTE THE TASK")

    def test_a_resume_continuation_is_not_re_enriched(self) -> None:
        runner = _CapturingRunner()
        adapter = self._adapter(runner, executor_contexts={"REC-01": _bundle()})
        adapter.launch(_request(resume_session_id="sess-1", fresh_session=False))
        self.assertEqual(runner.prompt, "EXECUTE THE TASK")

    def test_a_tampered_bundle_fails_the_launch_closed(self) -> None:
        bad = ExecutorContextBundle("REC-01", (
            ContextEntry("task", "docs/tasks/REC-01.md", _sha("real"), "tampered"),
        ))
        runner = _CapturingRunner()
        adapter = self._adapter(runner, executor_contexts={"REC-01": bad})
        with self.assertRaises(AdapterError) as caught:
            adapter.launch(_request())
        self.assertEqual(caught.exception.code, CONTEXT_BUNDLE_INVALID)
        self.assertIsNone(runner.prompt)  # failed before the process ran

    def test_bundle_does_not_widen_write_roots_or_relax_the_tool_policy(self) -> None:
        # The bundle travels on stdin; it must never become an --add-dir write root, and the
        # structural write denials for a read-only launch stay intact.
        argv = build_claude_argv(_request(role="task_verifier", read_only=True,
                                          tools=("Read",), role_grant=("read",)),
                                 executable="claude")
        self.assertNotIn("--add-dir", argv)
        disallowed = argv[argv.index("--disallowed-tools") + 1]
        self.assertIn("Edit", disallowed)
        self.assertIn("Write", disallowed)
        self.assertIn("Bash(git push:*)", disallowed)


# --- runner-owned assembly from the project tree ------------------------------------------


class BuildExecutorContextBundlesTests(unittest.TestCase):
    def _project(self, root: Path) -> tuple[Path, Path]:
        (root / "docs" / "plans" / "tasks").mkdir(parents=True)
        (root / ".prompts").mkdir()
        (root / ".agents" / "skills" / "tdd").mkdir(parents=True)
        plan = root / "docs" / "plans" / "routing.md"
        plan.write_text("# plan\n", encoding="utf-8")
        (root / ".prompts" / "feature.md").write_text("feature prompt\n", encoding="utf-8")
        (root / ".agents" / "skills" / "tdd" / "SKILL.md").write_text(
            "tdd skill\n", encoding="utf-8")
        (root / "docs" / "plans" / "tasks" / "REC-01_x.md").write_text(
            f"# REC-01\nwork under {root}\\src\n", encoding="utf-8")
        return plan, root / ".prompts" / "feature.md"

    def _spec(self) -> TaskSpec:
        return TaskSpec.build(
            id="REC-01", task_type="python", executor="python-executor",
            allowed_scope=["src/feature_pipeline/bootstrap.py"],
            acceptance_criteria=["AC-1"], path="docs/plans/tasks/REC-01_x.md",
            required_skills=[".agents/skills/tdd/SKILL.md"],
        )

    def test_assembles_a_validated_four_part_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            bundles = build_executor_context_bundles(
                [self._spec()], project_dir=root, agents_root=root / ".agents",
                plan_path=plan, prompt_path=prompt)
        self.assertIn("REC-01", bundles)
        bundle = bundles["REC-01"]
        bundle.validate()
        self.assertEqual({e.kind for e in bundle.entries},
                         {"task", "plan", "prompt", "skill"})
        for entry in bundle.entries:
            self.assertEqual(entry.digest, _sha(entry.content))
            self.assertNotIn("\\", entry.logical_source)

    def test_host_root_in_a_source_file_is_redacted_before_it_is_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            bundles = build_executor_context_bundles(
                [self._spec()], project_dir=root, agents_root=root / ".agents",
                plan_path=plan, prompt_path=prompt)
        task_entry = next(e for e in bundles["REC-01"].entries if e.kind == "task")
        self.assertNotIn(str(root), task_entry.content)
        task_entry.validate()

    def test_a_task_with_an_unreadable_contract_gets_no_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            spec = TaskSpec.build(
                id="GN-01", task_type="python", executor="python-executor",
                allowed_scope=["src/x.py"], acceptance_criteria=["AC"],
                path="docs/plans/tasks/GN-01_missing.md")
            bundles = build_executor_context_bundles(
                [spec], project_dir=root, agents_root=root / ".agents",
                plan_path=plan, prompt_path=prompt)
        self.assertEqual(bundles, {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
