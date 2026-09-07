"""Offline regression coverage for executable task preconditions."""

from __future__ import annotations

import subprocess
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pipeline_core.preconditions import Precondition, bind_refs, evaluate_preconditions, parse_preconditions
from pipeline_core.execution import ExecuteControls, TaskRunResult, execute_run
from pipeline_core.state import ACTOR_HUMAN, ACTOR_RUNNER, Run
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.adapters import ClaudeAdapter
from tests.test_execute_mode import _request, _specs, sa
from pipeline_core.verification import VerifierLaunchers


class PreconditionsTests(unittest.TestCase):
    def test_core_gitlink_uses_gitlink_binding_and_peeled_tag(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv, **_kwargs):
            calls.append(tuple(argv))
            self.assertEqual(_kwargs["cwd"], Path("vendor/core"))
            self.assertEqual(_kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(_kwargs["env"]["GCM_INTERACTIVE"], "Never")
            self.assertIn("BatchMode=yes", _kwargs["env"]["GIT_SSH_COMMAND"])
            if argv[1] == "rev-parse":
                self.fail("persisted binding must avoid a fresh local lookup")
            return subprocess.CompletedProcess(argv, 0, "gitlink-sha refs/tags/v1^{}\n")

        failed = evaluate_preconditions(
            (Precondition("ref-published", "core-gitlink"),),
            repo_root=Path("."), grants=(), approvals=(),
            core_root=Path("vendor/core"),
            published_refs={"core-gitlink": "refs/tags/v1"},
            expected_refs={"core-gitlink": "gitlink-sha"}, run=fake_run,
        )

        self.assertIsNone(failed)
        self.assertEqual(calls[0][-2:], ("refs/tags/v1", "refs/tags/v1^{}"))

    def test_transport_failure_fails_closed(self) -> None:
        def fake_run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("git", 15)

        failed = evaluate_preconditions(
            (Precondition("ref-published", "parent-head"),),
            repo_root=Path("."), grants=(), approvals=(),
            published_refs={"parent-head": "refs/heads/main"},
            expected_refs={"parent-head": "local-sha"}, run=fake_run,
        )

        self.assertIsNotNone(failed)
        self.assertIn("remote read failed", failed[1])

    def test_moved_ref_requires_exact_sha(self) -> None:
        def fake_run(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 0, "moved-sha refs/heads/main\n")

        failed = evaluate_preconditions(
            (Precondition("ref-published", "parent-head"),),
            repo_root=Path("."), grants=(), approvals=(),
            published_refs={"parent-head": "refs/heads/main"},
            expected_refs={"parent-head": "local-sha"}, run=fake_run,
        )

        self.assertIsNotNone(failed)
        self.assertIn("expected local-sha, observed moved-sha", failed[1])

    def test_missing_mapping_and_missing_ref_fail_closed(self):
        predicate = Precondition("ref-published", "parent-head")
        for refs, output in (({}, ""), ({"parent-head": "refs/heads/main"}, ""),
                             ({"parent-head": "refs/heads/main"}, "sha refs/heads/unrelated\n")):
            with self.subTest(refs=refs, output=output):
                failed = evaluate_preconditions(
                    (predicate,), repo_root=Path("."), grants=(), approvals=(),
                    published_refs=refs, expected_refs={"parent-head": "sha"},
                    run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output),
                )
                self.assertIn("missing", failed[1])

    def test_authentication_failure_cannot_pass(self):
        def denied(argv, **kwargs):
            raise subprocess.CalledProcessError(128, argv)
        failed = evaluate_preconditions(
            (Precondition("ref-published", "parent-head"),), repo_root=Path("."),
            grants=(), approvals=(), published_refs={"parent-head": "refs/heads/main"},
            expected_refs={"parent-head": "sha"}, run=denied,
        )
        self.assertIn("remote read failed", failed[1])

    def test_binding_uses_the_explicit_core_anchor_gitlink(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def read(argv, **kwargs):
                calls.append((argv, kwargs["cwd"]))
                return subprocess.CompletedProcess(argv, 0, "160000 commit reviewed\tvendor/runtime\n")
            bindings = bind_refs((Precondition("ref-published", "core-gitlink"),),
                                 repo_root=root, core_root=root / "vendor/runtime", run=read)
            self.assertEqual(bindings, {"core-gitlink": "reviewed"})
            self.assertEqual(calls, [(["git", "ls-tree", "HEAD", "--", "vendor/runtime"], root)])

    def test_non_gitlink_core_anchor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not a reviewed gitlink"):
            bind_refs((Precondition("ref-published", "core-gitlink"),),
                      repo_root=Path("."), core_root=Path("vendor/runtime"),
                      run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "040000 tree sha\tvendor/runtime\n"))


class DispatchPreconditionsTests(unittest.TestCase):
    @staticmethod
    def complete(life, _plan, task_id, _request):
        life.transition(task_id, "running", actor=ACTOR_RUNNER)
        life.transition(task_id, "implemented", actor=ACTOR_RUNNER)
        life.run.record_verdicts(task_id, "PASS", "PASS")
        return TaskRunResult(task_id, "verified", 0, 1, None, None, ())

    def request(self, root, *, predicates=(), controls=None, task_ids=("EX-01",)):
        scenario = sa.SCENARIOS["direct-success"]
        specs = list(_specs(task_ids))
        specs[-1] = replace(specs[-1], preconditions=predicates)
        for spec in specs:
            path = root / spec.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {spec.id}\n", encoding="utf-8")
        return _request(
            root, tuple(specs), executor=scenario.executor(),
            launchers=VerifierLaunchers(task=scenario.task_verifier(), test=scenario.test_verifier()),
            controls=controls or ExecuteControls(plan_approved=True), environment={"claude": True},
        )

    def test_later_ready_dependency_is_checked_before_dispatch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request = self.request(root, predicates=(Precondition("approval", "review"),),
                                   task_ids=("EX-01", "EX-02"))
            from pipeline_core.execution import TaskRunResult

            def complete(life, _plan, task_id, _request):
                self.assertEqual(task_id, "EX-01", "dependent bypassed its precondition")
                life.transition(task_id, "running", actor=ACTOR_RUNNER)
                life.transition(task_id, "implemented", actor=ACTOR_RUNNER)
                life.run.record_verdicts(task_id, "PASS", "PASS")
                return TaskRunResult(task_id, "verified", 0, 1, None, None, ())

            with patch("pipeline_core.execution._run_selected_task", side_effect=complete):
                result = execute_run(request)
            self.assertEqual(result.exit_code, 20)
            run = Run.load(request.run_dir, root)
            self.assertEqual(run.task("EX-02").status, "blocked")
            self.assertIn("approval: review", result.message)

    def test_unmet_precondition_suppresses_dependents_without_a_launch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request = self.request(root, task_ids=("EX-01", "EX-02"))
            first, second = request.specs
            request = replace(request, specs=(replace(first, preconditions=(Precondition("capability", "repo-admin"),)), second))
            result = execute_run(request)
            self.assertEqual(result.exit_code, 20)
            self.assertEqual(request.adapter.launches, 0)
            run = Run.load(request.run_dir, root)
            self.assertEqual(run.status, "blocked")
            self.assertEqual(run.task(first.id).status, "blocked")
            self.assertEqual(run.task(second.id).blocker, "blocked_by: EX-01")
            observation = run.controls["precondition_observations"]["value"][0]
            self.assertEqual(observation["predicate"], "capability: repo-admin")
            self.assertFalse(observation["passed"])

    def test_satisfied_predicates_preserve_normal_dispatch(self):
        with TemporaryDirectory() as directory:
            request = self.request(Path(directory),
                predicates=(Precondition("approval", "review"), Precondition("capability", "repo-admin")),
                controls=ExecuteControls(plan_approved=True, approvals=("review",), grants=("repo-admin",)))
            result = execute_run(request)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(request.adapter.launches, 1)

    def test_missing_named_executor_exits_30_without_running_task(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request = self.request(root)
            adapter = ClaudeAdapter(executable="claude", working_root=root)
            request = replace(request, adapter=adapter,
                              specs=(replace(request.specs[0], executor="missing-release-manager"),))
            with patch.object(adapter, "launch", side_effect=AssertionError("must not launch")):
                result = execute_run(request)
            self.assertEqual(result.exit_code, 30)
            self.assertIn("unresolved-executor", result.message)
            self.assertNotEqual(Run.load(request.run_dir, root).task("EX-01").status, "running")

    def test_available_named_agent_routes_after_preconditions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / ".claude/agents/release-manager.md"
            agent.parent.mkdir(parents=True)
            agent.write_text("---\nname: release-manager\n---\n", encoding="utf-8")
            request = self.request(root)
            request = replace(request, adapter=ClaudeAdapter(executable="claude", working_root=root),
                              specs=(replace(request.specs[0], executor="release-manager"),))
            with patch("pipeline_core.execution._run_selected_task", side_effect=self.complete) as dispatch:
                self.assertEqual(execute_run(request).exit_code, 0)
            self.assertEqual(dispatch.call_count, 1)

    def test_resolution_and_dispatch_share_the_selected_working_root(self):
        from feature_pipeline.application.compile_plan import compile_run_plan
        from feature_pipeline.domain.models import MARKDOWN_TASK_FILE, TaskDefinition
        from feature_pipeline.domain.paths import RelativePath
        from tests.test_execution_plan_compiler import _profile

        for compiled in (False, True):
            with self.subTest(compiled=compiled), TemporaryDirectory() as directory:
                root = Path(directory)
                agent = root / "workspace/task/.claude/agents/release-manager.md"
                agent.parent.mkdir(parents=True)
                agent.write_text("---\nname: release-manager\n---\n", encoding="utf-8")
                request = self.request(root)
                scripted = request.adapter
                spec = replace(request.specs[0], executor="release-manager")
                adapter = ClaudeAdapter(executable="claude", working_root=root)
                plan = None
                if compiled:
                    plan = compile_run_plan(
                        feature=request.feature,
                        definitions=(TaskDefinition(spec, MARKDOWN_TASK_FILE),),
                        profile=_profile(),
                    )
                    plan = replace(plan, tasks=(replace(
                        plan.tasks[0], working_root=RelativePath("workspace/task")),))
                request = replace(
                    request, specs=(spec,), adapter=adapter, compiled_plan=plan,
                    working_root="." if compiled else "workspace/task",
                )

                def launch(launch_request):
                    self.assertEqual(adapter._cwd_for(launch_request), root / "workspace/task")
                    self.assertEqual(launch_request.role, "release-manager")
                    return scripted.launch(launch_request)

                with patch.object(adapter, "launch", side_effect=launch):
                    result = execute_run(request)
                self.assertEqual(result.exit_code, 0, result.message)
                self.assertEqual(scripted.launches, 1)

    def test_builtin_executor_dispatches_without_an_agent_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request = self.request(root)
            scripted = request.adapter
            adapter = ClaudeAdapter(executable="claude", working_root=root, env={})
            request = replace(request, adapter=adapter,
                              specs=(replace(request.specs[0], executor="general-purpose"),))
            with patch.object(adapter, "launch", side_effect=scripted.launch):
                result = execute_run(request)
            self.assertEqual(result.exit_code, 0, result.message)
            self.assertEqual(scripted.launches, 1)

    def test_claude_builtins_resolve_without_files_and_respect_disable_controls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("pathlib.Path.home", return_value=root):
                for name in ("general-purpose", "Explore", "Plan", "claude",
                             "statusline-setup", "claude-code-guide"):
                    with self.subTest(name=name):
                        adapter = ClaudeAdapter(working_root=root, env={})
                        self.assertTrue(adapter.can_resolve_executor(name))
                        adapter = ClaudeAdapter(working_root=root, env={
                            "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1"})
                        self.assertFalse(adapter.can_resolve_executor(name))
                adapter = ClaudeAdapter(working_root=root, env={
                    "CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS": "1"})
                self.assertFalse(adapter.can_resolve_executor("Explore"))
                self.assertFalse(adapter.can_resolve_executor("Plan"))
                self.assertTrue(adapter.can_resolve_executor("general-purpose"))

    def test_unmet_published_ref_blocks_before_launch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request = self.request(root, predicates=(Precondition("ref-published", "parent-head"),))

            def local_only(argv, **kwargs):
                self.assertNotIn("ls-remote", argv, "missing mapping needs no remote lookup")
                return subprocess.CompletedProcess(argv, 0, "reviewed-sha")

            request = replace(request, precondition_runner=local_only)
            result = execute_run(request)
            self.assertEqual(result.exit_code, 20)
            self.assertEqual(request.adapter.launches, 0)
            self.assertEqual(Run.load(request.run_dir, root).task("EX-01").status, "blocked")
            self.assertIn("ref-published: parent-head", result.message)

    def test_removed_executor_blocks_repair_routing_without_another_launch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / ".claude/agents/release-manager.md"
            agent.parent.mkdir(parents=True)
            agent.write_text("---\nname: release-manager\n---\n", encoding="utf-8")
            request = self.request(root)
            scenario = sa.SCENARIOS["successful-repair"]
            scripted = scenario.executor()
            adapter = ClaudeAdapter(executable="claude", working_root=root)
            request = replace(request, adapter=adapter,
                specs=(replace(request.specs[0], executor="release-manager"),),
                launchers=VerifierLaunchers(task=scenario.task_verifier(), test=scenario.test_verifier()))

            def launch(launch_request):
                result = scripted.launch(launch_request)
                if agent.exists():
                    agent.unlink()
                return result

            with patch.object(adapter, "launch", side_effect=launch):
                result = execute_run(request)
            self.assertEqual(result.exit_code, 30, result.message)
            self.assertIn("unresolved-executor", result.message)
            self.assertEqual(scripted.launches, 1)
            run = Run.load(request.run_dir, root)
            self.assertEqual(run.task("EX-01").status, "blocked")
            self.assertEqual(run.status, "blocked")

    def test_blocked_resume_requires_human_recovery_and_fresh_approval(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request = self.request(root, predicates=(Precondition("approval", "review"),))
            self.assertEqual(execute_run(request).exit_code, 20)
            approved = replace(request, controls=replace(request.controls, resume=True, approvals=("review",)))
            self.assertEqual(execute_run(approved).exit_code, 20)
            life = RunLifecycle.load(request.run_dir, root)
            life.transition("EX-01", "ready", actor=ACTOR_HUMAN)
            self.assertEqual(execute_run(replace(request, controls=replace(request.controls, resume=True))).exit_code, 20)
            life = RunLifecycle.load(request.run_dir, root)
            life.transition("EX-01", "ready", actor=ACTOR_HUMAN)
            with patch("pipeline_core.execution._run_selected_task", side_effect=self.complete):
                self.assertEqual(execute_run(approved).exit_code, 0)

    def test_resumed_repair_cannot_skip_preconditions(self):
        for status in ("implemented", "verification_failed", "repairing"):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                root = Path(directory)
                request = self.request(root, predicates=(Precondition("approval", "review"),))
                self.assertEqual(execute_run(request).exit_code, 20)
                life = RunLifecycle.load(request.run_dir, root)
                life.transition("EX-01", "ready", actor=ACTOR_HUMAN)
                life.transition("EX-01", "running", actor=ACTOR_RUNNER)
                life.transition("EX-01", "implemented", actor=ACTOR_RUNNER)
                if status != "implemented":
                    life.transition("EX-01", "verification_failed", actor=ACTOR_RUNNER)
                if status == "repairing":
                    life.transition("EX-01", "repairing", actor=ACTOR_RUNNER)
                with patch("pipeline_core.execution._run_selected_task", side_effect=AssertionError("must not dispatch")):
                    result = execute_run(replace(request, controls=replace(request.controls, resume=True)))
                self.assertEqual(result.exit_code, 20)
                self.assertIn("missing approval", result.message)

    def test_new_predicate_cannot_retain_verified_contract_on_resume(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request = self.request(root)
            with patch("pipeline_core.execution._run_selected_task", side_effect=self.complete):
                self.assertEqual(execute_run(request).exit_code, 0)
            before = (request.run_dir / "run.json").read_bytes()
            changed = replace(request, controls=replace(request.controls, resume=True),
                              specs=(replace(request.specs[0], preconditions=(Precondition("approval", "new-review"),)),))
            result = execute_run(changed)
            self.assertEqual(result.exit_code, 30)
            self.assertIn("task-contract-mismatch", result.message)
            self.assertEqual((request.run_dir / "run.json").read_bytes(), before)

    def test_verified_resume_rechecks_current_controls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request = self.request(root, predicates=(Precondition("approval", "review"),),
                                   controls=ExecuteControls(plan_approved=True, approvals=("review",)))
            with patch("pipeline_core.execution._run_selected_task", side_effect=self.complete):
                self.assertEqual(execute_run(request).exit_code, 0)
            result = execute_run(replace(request, controls=replace(request.controls, resume=True, approvals=())))
            self.assertEqual(result.exit_code, 20)
            self.assertEqual(Run.load(request.run_dir, root).task("EX-01").status, "blocked")

    def test_legacy_run_without_predicate_controls_resumes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request = self.request(root)
            with patch("pipeline_core.execution._run_selected_task", side_effect=self.complete):
                self.assertEqual(execute_run(request).exit_code, 0)
            run = Run.load(request.run_dir, root)
            run.controls.pop("precondition_bindings")
            run.controls.pop("preconditions")
            run.save()
            self.assertEqual(execute_run(replace(request, controls=replace(request.controls, resume=True))).exit_code, 0)

    def test_resume_rejects_moved_local_sha_or_changed_mapping_without_writes(self):
        for moved in ("sha", "mapping"):
            with self.subTest(moved=moved), TemporaryDirectory() as directory:
                root = Path(directory)
                def git(argv, **kwargs):
                    return subprocess.CompletedProcess(argv, 0, "sha" if "rev-parse" in argv else "sha refs/heads/main\n")
                request = self.request(root, predicates=(Precondition("ref-published", "parent-head"),),
                    controls=ExecuteControls(plan_approved=True, published_refs=(("parent-head", "refs/heads/main"),)))
                request = replace(request, precondition_runner=git)
                with patch("pipeline_core.execution._run_selected_task", side_effect=self.complete):
                    self.assertEqual(execute_run(request).exit_code, 0)
                before = (request.run_dir / "run.json").read_bytes()
                resumed = replace(request, controls=replace(request.controls, resume=True))
                if moved == "sha":
                    resumed = replace(resumed, precondition_runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "changed"))
                else:
                    resumed = replace(resumed, controls=replace(resumed.controls, published_refs=(("parent-head", "refs/heads/other"),)))
                result = execute_run(resumed)
                self.assertEqual(result.exit_code, 30)
                self.assertIn("precondition-binding-mismatch", result.message)
                self.assertEqual((request.run_dir / "run.json").read_bytes(), before)

    def test_resume_rechecks_remote_tip_with_the_bound_sha(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            remote = "sha"
            calls = []
            def git(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "sha" if "rev-parse" in argv else f"{remote} refs/heads/main\n")
            request = self.request(root, predicates=(Precondition("ref-published", "parent-head"),),
                controls=ExecuteControls(plan_approved=True, published_refs=(("parent-head", "refs/heads/main"),)))
            request = replace(request, precondition_runner=git)
            with patch("pipeline_core.execution._run_selected_task", side_effect=self.complete):
                self.assertEqual(execute_run(request).exit_code, 0)
            calls.clear()
            remote = "moved"
            result = execute_run(replace(request, controls=replace(request.controls, resume=True)))
            self.assertEqual(result.exit_code, 20)
            self.assertTrue(any("ls-remote" in argv for argv in calls))
            self.assertIn("expected sha, observed moved", result.message)

    def test_reused_dependency_checks_consumers_approval_and_preserves_source(self):
        from tests.test_verified_reuse import _source
        from feature_pipeline.domain.models import TaskDefinition, MARKDOWN_TASK_FILE
        for approved in (False, True):
            with self.subTest(approved=approved), TemporaryDirectory() as directory:
                root = Path(directory)
                request = self.request(root, task_ids=("EX-01", "EX-02"),
                    controls=ExecuteControls(plan_approved=True, task="EX-02", approvals=("review",) if approved else ()))
                first, second = request.specs
                first = replace(first, preconditions=(Precondition("approval", "review"),))
                request = replace(request, specs=(first, second))
                source = _source(root, run_id="source", definition=TaskDefinition(first, MARKDOWN_TASK_FILE))
                before = source.read_bytes()
                with patch("pipeline_core.execution._run_selected_task", side_effect=self.complete) as dispatch:
                    result = execute_run(request)
                self.assertEqual(result.exit_code, 0 if approved else 20)
                self.assertEqual(source.read_bytes(), before)
                run = Run.load(request.run_dir, root)
                self.assertEqual(bool(run.task("EX-01").reused_verification), approved)
                self.assertEqual([call.args[2] for call in dispatch.call_args_list], ["EX-02"] if approved else [])
                self.assertEqual(run.controls["precondition_observations"]["value"][0]["passed"], approved)

    def test_remote_move_blocks_a_repair_redispatch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scenario = sa.SCENARIOS["successful-repair"]
            executor = scenario.executor()
            def git(argv, **kwargs):
                if "rev-parse" in argv:
                    output = "sha"
                else:
                    output = f"{'moved' if executor.launches else 'sha'} refs/heads/main\n"
                return subprocess.CompletedProcess(argv, 0, output)
            request = self.request(root, predicates=(Precondition("ref-published", "parent-head"),),
                controls=ExecuteControls(plan_approved=True, published_refs=(("parent-head", "refs/heads/main"),)))
            request = replace(request, adapter=executor, precondition_runner=git,
                launchers=VerifierLaunchers(task=scenario.task_verifier(), test=scenario.test_verifier()))
            result = execute_run(request)
            self.assertEqual(result.exit_code, 20)
            self.assertEqual(executor.launches, 1)
            self.assertIn("observed moved", result.message)


class PreconditionContractTests(unittest.TestCase):
    def test_declared_defaulted_json_and_legacy_inputs_preserve_predicates(self):
        from tests.test_input_normalization import DECLARED_TASK_MD, HISTORICAL_TASK_MD, DEFAULTS, EQUIVALENT_JSON_ENTRY
        from feature_pipeline.inputs import TaskDefinitionBuilder
        from pipeline_core.legacy_adapter import adapt_task_spec
        predicate = Precondition("approval", "review")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text, defaults in (("AB-01_declared.md", DECLARED_TASK_MD, None),
                                          ("LT-09_historical.md", HISTORICAL_TASK_MD, DEFAULTS)):
                path = root / name
                path.write_text(text + "\n## Preconditions\n- approval: review\n", encoding="utf-8")
                definition = TaskDefinitionBuilder.from_markdown_task_file(path, defaults=defaults)
                self.assertEqual(definition.preconditions, (predicate,))
            entry = {**EQUIVALENT_JSON_ENTRY, "preconditions": [{"kind": "approval", "value": "review"}]}
            self.assertEqual(TaskDefinitionBuilder.from_json_plan_entry(entry).preconditions, (predicate,))
            self.assertEqual(adapt_task_spec(entry).preconditions, (predicate,))

    def test_invalid_predicates_fail_markdown_plan_loading(self):
        from feature_pipeline.contracts import SchemaError
        from pipeline_core.plan_md import MarkdownPlanError, load_markdown_plan, load_markdown_plan_specs
        from tests.test_input_normalization import DECLARED_TASK_MD
        from feature_pipeline.inputs import TaskDefinitionBuilder
        for declaration in ("- unknown: review", "- ref-published: unknown", "- approval:", "approval: review"):
            with self.subTest(declaration=declaration), TemporaryDirectory() as directory:
                root = Path(directory)
                plan = root / "plan.md"
                plan.write_text("# Plan\n## Phase 1\n### AB-01\n", encoding="utf-8")
                task = root / "tasks/AB-01_declared.md"
                task.parent.mkdir()
                task.write_text(DECLARED_TASK_MD + "\n## Preconditions\n" + declaration + "\n", encoding="utf-8")
                for load in (load_markdown_plan, load_markdown_plan_specs):
                    with self.assertRaises(MarkdownPlanError):
                        load(plan)
                with self.assertRaises(SchemaError):
                    TaskDefinitionBuilder.from_markdown_task_file(task)

    def test_compiled_predicates_change_resume_identity(self):
        from tests.test_execution_plan_compiler import _profile, _definition
        from feature_pipeline.application.compile_plan import compile_run_plan
        from pipeline_core.execution import _ensure_plan_compatible, _plan_fingerprint, ExecutionError
        definition = _definition("AB-01")
        original = compile_run_plan(feature="sample", definitions=(definition,), profile=_profile())
        changed = replace(definition, spec=replace(definition.spec, preconditions=(Precondition("approval", "review"),)))
        current = compile_run_plan(feature="sample", definitions=(changed,), profile=_profile())
        self.assertEqual(current.task("AB-01").preconditions, changed.preconditions)
        self.assertNotEqual(original.digest, current.digest)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = Run.create("sample", "prompt.md", "plan.md", root / "runs/sample", root)
            run.set_control("plan_fingerprint", _plan_fingerprint(original))
            with self.assertRaisesRegex(ExecutionError, "preconditions"):
                _ensure_plan_compatible(run, current)

    def test_legacy_evidence_cannot_satisfy_a_new_predicate(self):
        from tests.test_verified_reuse import _definition, _source
        from feature_pipeline.application.verified_reuse import VerifiedEvidenceStore, EvidenceEligibilityError, task_contract_digest
        definition = _definition(depends_on=())
        changed = replace(definition, spec=replace(definition.spec, preconditions=(Precondition("approval", "review"),)))
        self.assertNotEqual(task_contract_digest(definition), task_contract_digest(changed))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = _source(root, run_id="legacy", definition=definition)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["tasks"][0].pop("task_contract_digest")
            path.write_text(json.dumps(payload), encoding="utf-8")
            store = VerifiedEvidenceStore(root / "runs", root)
            for lookup in (lambda: store.find(changed), lambda: store.find_at(path.parent, changed)):
                with self.assertRaises(EvidenceEligibilityError):
                    lookup()

    def test_dry_run_displays_unresolved_predicates_without_processes_or_writes(self):
        from tests.test_runner_cli import _seed, _run
        with TemporaryDirectory() as directory:
            root = Path(directory)
            seed = _seed("library-guide", root, tasks=[{
                "id": "AB-01", "type": "docs", "executor": "docs-executor",
                "allowed_scope": ["content/**"], "acceptance_criteria": ["done"],
                "preconditions": [{"kind": "ref-published", "value": "parent-head"}],
            }])
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with patch("subprocess.run", side_effect=AssertionError("dry run must not launch a process")):
                code, output, error = _run(seed["anchors"] + ["--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run"])
            self.assertEqual(code, 10, error)
            self.assertIn("ref-published: parent-head", output)
            self.assertIn("unresolved (execute-time evaluation required)", output)
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before, after)

    def test_invalid_predicates_are_not_hidden_by_preview_fallback(self):
        from tests.test_runner_cli import _seed, _run
        for rich in (False, True):
            with self.subTest(rich=rich), TemporaryDirectory() as directory:
                task = {"id": "AB-01", "type": "docs", "preconditions": [{"kind": "unknown", "value": "x"}]}
                if rich:
                    task.update(executor="docs-executor", allowed_scope=["content/**"], acceptance_criteria=["done"])
                seed = _seed("library-guide", Path(directory), tasks=[task])
                code, _, error = _run(seed["anchors"] + ["--profile", seed["profile_rel"], "--plan", "plan.json", "--dry-run"])
                self.assertEqual(code, 30)
                self.assertIn("precondition", error)
