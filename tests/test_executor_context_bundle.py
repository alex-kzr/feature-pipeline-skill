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

from feature_pipeline.bootstrap import build_executor_context_bundles, build_required_input_dirs
from feature_pipeline.contracts import TaskSpec
from pipeline_core.adapters import (
    CONTEXT_BUNDLE_INVALID,
    CONTEXT_UNAVAILABLE,
    AdapterError,
    ClaudeAdapter,
    CodexAdapter,
    CompletedProcess,
    ContextEntry,
    ExecutorContextBundle,
    _materialize_container_context,
    LaunchRequest,
    build_claude_argv,
)
from tests.support.isolation import proven_isolation_capabilities


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
        for leaky in (
            r"see C:\Users\admin\secret", "cd /home/admin/project",
            r"open \\server\share\secret",
        ):
            with self.subTest(content=leaky):
                with self.assertRaises(AdapterError) as caught:
                    ContextEntry.of("plan", "docs/plan.md", leaky)
                self.assertEqual(caught.exception.code, CONTEXT_BUNDLE_INVALID)

    def test_generic_drive_root_documentation_literal_is_allowed_unless_configured(self) -> None:
        entry = ContextEntry.of("plan", "docs/plan.md", "Example: C:/docs/x\n")
        self.assertEqual(entry.content, "Example: C:/docs/x\n")
        with self.assertRaises(AdapterError) as caught:
            ContextEntry.of(
                "plan", "docs/plan.md", "Example: C:/docs/x\n",
                host_roots=("C:/docs",),
            )
        self.assertEqual(caught.exception.code, CONTEXT_BUNDLE_INVALID)

    def test_source_code_newline_escape_is_not_mistaken_for_a_windows_path(self) -> None:
        ContextEntry.of("input", "feature-pipeline-skill/pipeline_core/dispatch.py",
                        'message = "contract:\\n"\n').validate()

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
        kw.setdefault("isolation_capabilities", proven_isolation_capabilities("claude"))
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

    def test_documentation_drive_example_is_bound_after_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            task = root / "docs" / "plans" / "tasks" / "REC-01_x.md"
            task.write_text("Example safety path: C:/docs/x\n", encoding="utf-8")

            bundle = build_executor_context_bundles(
                [self._spec()], project_dir=root, agents_root=root / ".agents",
                plan_path=plan, prompt_path=prompt,
            )["REC-01"]

        task_entry = next(entry for entry in bundle.entries if entry.kind == "task")
        self.assertEqual(task_entry.content, "Example safety path: C:/docs/x\n")

    def test_invalid_context_content_is_not_classified_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            task = root / "docs" / "plans" / "tasks" / "REC-01_x.md"
            task.write_text(r"UNC leak: \\server\share\secret", encoding="utf-8")

            with self.assertRaises(AdapterError) as caught:
                build_executor_context_bundles(
                    [self._spec()], project_dir=root, agents_root=root / ".agents",
                    plan_path=plan, prompt_path=prompt,
                )

        self.assertEqual(caught.exception.code, CONTEXT_BUNDLE_INVALID)

    def test_tc11_declared_prerequisites_are_bundled_without_a_project_tree_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            proposal = root / ".prompts" / "proposal.md"
            research = root / ".prompts" / "research.md"
            proposal.write_text("proposal\n", encoding="utf-8")
            research.write_text("research\n", encoding="utf-8")
            review = root / "docs" / "validation" / "routing" / "TC-09-review.md"
            review.parent.mkdir(parents=True)
            review.write_text("review\n", encoding="utf-8")
            (root / "docs" / "plans" / "tasks" / "TC-09_x.md").write_text(
                "# TC-09\n", encoding="utf-8"
            )
            task = root / "docs" / "plans" / "tasks" / "TC-11_x.md"
            task.write_text(
                "# TC-11\n"
                "Read the [proposal](../../../.prompts/proposal.md) and "
                "[research](../../../.prompts/research.md), plus the linked plan and the "
                "latest preceding review report.\n",
                encoding="utf-8",
            )
            prior = TaskSpec.build(
                id="TC-09", title="Review routing", task_type="docs", executor="executor",
                allowed_scope=["docs/validation/routing/TC-09-review.md"],
                acceptance_criteria=["AC"], path="docs/plans/tasks/TC-09_x.md",
            )
            spec = TaskSpec.build(
                id="TC-11", task_type="python", executor="python-executor",
                allowed_scope=["src/x.py"], acceptance_criteria=["AC"],
                path="docs/plans/tasks/TC-11_x.md",
            )
            bundles = build_executor_context_bundles(
                [spec], project_dir=root, agents_root=root / ".agents",
                plan_path=plan, prompt_path=prompt, task_ids=("TC-11",),
                plan_specs=[prior, spec],
            )
            input_dirs = build_required_input_dirs(
                [spec], project_dir=root, agents_root=root / ".agents",
                plan_path=plan, prompt_path=prompt,
                task_ids=("TC-11",), plan_specs=[prior, spec],
                working_root_by_id={"TC-11": "feature-pipeline-skill"},
            )

        sources = {entry.logical_source for entry in bundles["TC-11"].entries}
        self.assertEqual(
            sources,
            {
                "docs/plans/tasks/TC-11_x.md", "docs/plans/routing.md",
                ".prompts/feature.md", ".prompts/proposal.md", ".prompts/research.md",
                "docs/validation/routing/TC-09-review.md",
            },
        )
        review_entry = next(
            entry for entry in bundles["TC-11"].entries
            if entry.logical_source == "docs/validation/routing/TC-09-review.md"
        )
        self.assertEqual(review_entry.content, "review\n")
        self.assertEqual(
            set(input_dirs["TC-11"]),
            {
                str((root / "docs" / "plans").resolve()),
                str((root / ".prompts").resolve()),
                str((root / "docs" / "validation" / "routing").resolve()),
            },
        )
        self.assertNotIn(str(root.resolve()), input_dirs["TC-11"])

    def test_missing_preceding_review_in_the_full_plan_fails_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            task = root / "docs" / "plans" / "tasks" / "TC-11_x.md"
            task.write_text(
                "# TC-11\nRead the latest preceding review report.\n",
                encoding="utf-8",
            )
            spec = TaskSpec.build(
                id="TC-11", task_type="python", executor="python-executor",
                allowed_scope=["src/x.py"], acceptance_criteria=["AC"],
                path="docs/plans/tasks/TC-11_x.md",
            )

            with self.assertRaises(AdapterError) as caught:
                build_executor_context_bundles(
                    [spec], project_dir=root, agents_root=root / ".agents",
                    plan_path=plan, prompt_path=prompt, task_ids=("TC-11",),
                    plan_specs=[spec],
                )

        self.assertEqual(caught.exception.code, CONTEXT_UNAVAILABLE)

    def test_real_tc11_bundles_each_declared_prerequisite_path(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        task_path = "docs/plans/tasks/TC-11_enforce-worker-isolation.md"
        spec = TaskSpec.build(
            id="TC-11", task_type="python", executor="python-executor",
            allowed_scope=["feature-pipeline-skill/src/feature_pipeline/bootstrap.py"],
            acceptance_criteria=["AC"], path=task_path,
        )
        prior_review = TaskSpec.build(
            id="TC-09", title="Review stack routing", task_type="docs", executor="executor",
            allowed_scope=["docs/validation/task-model-routing/TC-09-review.md"],
            acceptance_criteria=["AC"], path="docs/plans/tasks/TC-09_review-r02.md",
        )

        bundles = build_executor_context_bundles(
            [prior_review, spec], project_dir=project_root,
            agents_root=project_root / ".agents",
            plan_path=project_root / "docs/plans/2026-09-08-universal-pipeline-task-model-routing.md",
            prompt_path=project_root / ".prompts/2026-09-07-universal-pipeline-task-model-routing-proposal.md",
            task_ids=("TC-11",),
            working_root_by_id={"TC-11": "feature-pipeline-skill"},
        )

        declared_paths = {
            ".prompts/2026-09-07-universal-pipeline-task-model-routing-proposal.md",
            ".prompts/2026-09-07-universal-pipeline-task-model-routing-research.md",
            "docs/validation/task-model-routing/TC-02-capabilities.md",
            "feature-pipeline-skill/src/feature_pipeline/ports/process.py",
        }
        entries = {entry.logical_source: entry for entry in bundles["TC-11"].entries}
        self.assertTrue(declared_paths.issubset(entries))
        for source in declared_paths:
            self.assertEqual(entries[source].content, (project_root / source).read_text(encoding="utf-8"))

        input_dirs = build_required_input_dirs(
            [prior_review, spec], project_dir=project_root,
            agents_root=project_root / ".agents",
            plan_path=project_root / "docs/plans/2026-09-08-universal-pipeline-task-model-routing.md",
            prompt_path=project_root / ".prompts/2026-09-07-universal-pipeline-task-model-routing-proposal.md",
            working_root_by_id={"TC-11": "feature-pipeline-skill"}, task_ids=("TC-11",),
        )
        self.assertTrue({
            str((project_root / ".prompts").resolve()),
            str((project_root / "docs/validation/task-model-routing").resolve()),
        }.issubset(input_dirs["TC-11"]))
        self.assertNotIn(
            str((project_root / "feature-pipeline-skill/src/feature_pipeline/ports").resolve()),
            input_dirs["TC-11"],
        )

    def test_working_root_relative_declared_path_is_bundled_from_the_nested_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            (root / "AGENTS.md").write_text("PROJECT INSTRUCTIONS\n", encoding="utf-8")
            worker_file = root / "feature-pipeline-skill" / "src" / "worker_input.py"
            worker_file.parent.mkdir(parents=True)
            worker_file.write_text("WORKER INPUT\n", encoding="utf-8")
            task = root / "docs" / "plans" / "tasks" / "REC-01_x.md"
            task.write_text(
                "# REC-01\n\n## Context\n\n- `AGENTS.md`\n- `src/worker_input.py`\n",
                encoding="utf-8",
            )

            bundles = build_executor_context_bundles(
                [self._spec()], project_dir=root, agents_root=root / ".agents",
                plan_path=plan, prompt_path=prompt,
                working_root_by_id={"REC-01": "feature-pipeline-skill"},
            )
            context_paths = _materialize_container_context(
                bundles["REC-01"], root / "container-context",
                runtime_root=Path(__file__).resolve().parents[1],
            )

        entries = {entry.logical_source: entry for entry in bundles["REC-01"].entries}
        self.assertEqual(entries["AGENTS.md"].content, "PROJECT INSTRUCTIONS\n")
        self.assertEqual(context_paths["AGENTS.md"], "/context/project/AGENTS.md")
        self.assertEqual(
            entries["feature-pipeline-skill/src/worker_input.py"].content,
            "WORKER INPUT\n",
        )

    def test_explicit_bundle_entry_wins_over_runtime_closure_source(self) -> None:
        """A bound source stays immutable when the import closure also needs its path."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_root = Path(__file__).resolve().parents[1]
            logical_source = (
                "feature-pipeline-skill/"
                "src/feature_pipeline/infrastructure/adapters/codex_launcher.py"
            )
            bound_content = "# runner-bound replacement\n"
            bundle = ExecutorContextBundle("REC-01", (
                ContextEntry.of("task", "docs/plans/tasks/REC-01.md", "TASK BODY"),
                ContextEntry.of("input", logical_source, bound_content),
            ))

            _materialize_container_context(
                bundle, root / "container-context", runtime_root=runtime_root,
            )

            bound_destination = root / "container-context" / "project" / logical_source
            self.assertEqual(bound_destination.read_text(encoding="utf-8"), bound_content)
            self.assertEqual(
                _sha(bound_destination.read_text(encoding="utf-8")),
                bundle.entries[1].digest,
            )
            dependency = root / "container-context" / "project" / "feature-pipeline-skill" / (
                "src/feature_pipeline/contracts.py"
            )
            self.assertEqual(
                dependency.read_bytes(),
                (runtime_root / "src/feature_pipeline/contracts.py").read_bytes(),
            )

    def test_source_location_suffix_resolves_the_working_root_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            worker_file = root / "feature-pipeline-skill" / "src" / "worker_input.py"
            worker_file.parent.mkdir(parents=True)
            worker_file.write_text("WORKER INPUT\n", encoding="utf-8")
            task = root / "docs" / "plans" / "tasks" / "REC-01_x.md"
            task.write_text(
                "# REC-01\n\n## Context\n\n- `src/worker_input.py:17:4`\n",
                encoding="utf-8",
            )

            bundles = build_executor_context_bundles(
                [self._spec()], project_dir=root, agents_root=root / ".agents",
                plan_path=plan, prompt_path=prompt,
                working_root_by_id={"REC-01": "feature-pipeline-skill"},
            )
            input_dirs = build_required_input_dirs(
                [self._spec()], project_dir=root, agents_root=root / ".agents",
                plan_path=plan, prompt_path=prompt,
                working_root_by_id={"REC-01": "feature-pipeline-skill"},
            )

        self.assertIn("feature-pipeline-skill/src/worker_input.py", {
            entry.logical_source for entry in bundles["REC-01"].entries
        })
        self.assertNotIn(
            str(worker_file.parent.resolve()), input_dirs["REC-01"],
        )

    def test_source_location_line_list_resolves_the_working_root_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            worker_file = root / "feature-pipeline-skill" / "src" / "worker_input.py"
            worker_file.parent.mkdir(parents=True)
            worker_file.write_text("WORKER INPUT\n", encoding="utf-8")
            task = root / "docs" / "plans" / "tasks" / "REC-01_x.md"
            task.write_text(
                "# REC-01\n\n## Context\n\n- `src/worker_input.py:17,23,41`\n",
                encoding="utf-8",
            )

            bundles = build_executor_context_bundles(
                [self._spec()], project_dir=root, agents_root=root / ".agents",
                plan_path=plan, prompt_path=prompt,
                working_root_by_id={"REC-01": "feature-pipeline-skill"},
            )

        self.assertIn("feature-pipeline-skill/src/worker_input.py", {
            entry.logical_source for entry in bundles["REC-01"].entries
        })

    def test_malformed_source_location_path_is_not_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            worker_file = root / "feature-pipeline-skill" / "src" / "worker_input.py"
            worker_file.parent.mkdir(parents=True)
            worker_file.write_text("WORKER INPUT\n", encoding="utf-8")
            task = root / "docs" / "plans" / "tasks" / "REC-01_x.md"
            task.write_text(
                "# REC-01\n\n## Context\n\n- `../feature-pipeline-skill/src/worker_input.py:17`\n",
                encoding="utf-8",
            )

            bundles = build_executor_context_bundles(
                [self._spec()], project_dir=root, agents_root=root / ".agents",
                plan_path=plan, prompt_path=prompt,
                working_root_by_id={"REC-01": "feature-pipeline-skill"},
            )

        self.assertNotIn("feature-pipeline-skill/src/worker_input.py", {
            entry.logical_source for entry in bundles["REC-01"].entries
        })

    def test_malformed_source_location_line_lists_are_rejected(self) -> None:
        malformed_sources = (
            "src/worker_input.py:17,",
            "src/worker_input.py:17,,23",
            "src/worker_input.py:17,twenty",
            "../feature-pipeline-skill/src/worker_input.py:17,23",
        )
        for source in malformed_sources:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan, prompt = self._project(root)
                worker_file = root / "feature-pipeline-skill" / "src" / "worker_input.py"
                worker_file.parent.mkdir(parents=True)
                worker_file.write_text("WORKER INPUT\n", encoding="utf-8")
                task = root / "docs" / "plans" / "tasks" / "REC-01_x.md"
                task.write_text(
                    f"# REC-01\n\n## Context\n\n- `{source}`\n", encoding="utf-8"
                )

                bundles = build_executor_context_bundles(
                    [self._spec()], project_dir=root, agents_root=root / ".agents",
                    plan_path=plan, prompt_path=prompt,
                    working_root_by_id={"REC-01": "feature-pipeline-skill"},
                )

            self.assertNotIn("feature-pipeline-skill/src/worker_input.py", {
                entry.logical_source for entry in bundles["REC-01"].entries
            })

    def test_a_missing_declared_prerequisite_fails_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            (root / "docs" / "plans" / "tasks" / "GN-01_missing.md").write_text(
                "# GN-01\nRead [required input](../../../.prompts/missing.md).\n",
                encoding="utf-8",
            )
            spec = TaskSpec.build(
                id="GN-01", task_type="python", executor="python-executor",
                allowed_scope=["src/x.py"], acceptance_criteria=["AC"],
                path="docs/plans/tasks/GN-01_missing.md")
            runner = _CapturingRunner()
            codex = CodexAdapter(executable="codex", runner=runner)
            with self.assertRaises(AdapterError) as caught:
                contexts = build_executor_context_bundles(
                    [spec], project_dir=root, agents_root=root / ".agents",
                    plan_path=plan, prompt_path=prompt)
                codex.launch(_request())  # pragma: no cover - context construction must stop first
        self.assertEqual(caught.exception.code, CONTEXT_UNAVAILABLE)
        self.assertIsNone(runner.prompt)

    def test_a_missing_working_root_relative_context_path_fails_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, prompt = self._project(root)
            (root / "docs" / "plans" / "tasks" / "GN-02_missing.md").write_text(
                "# GN-02\n\n## Context\n\n- `src/missing.py`\n", encoding="utf-8"
            )
            spec = TaskSpec.build(
                id="GN-02", task_type="python", executor="python-executor",
                allowed_scope=["src/x.py"], acceptance_criteria=["AC"],
                path="docs/plans/tasks/GN-02_missing.md",
            )

            with self.assertRaises(AdapterError) as caught:
                build_executor_context_bundles(
                    [spec], project_dir=root, agents_root=root / ".agents",
                    plan_path=plan, prompt_path=prompt,
                    working_root_by_id={"GN-02": "feature-pipeline-skill"},
                )

        self.assertEqual(caught.exception.code, CONTEXT_UNAVAILABLE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
