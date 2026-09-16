"""TC-01: capture current dispatch counts, session policy, repair and durable reuse.

Adapters are local doubles. Their PASS tokens exercise runner control flow and are
not independent acceptance of TC-01 or evidence of live provider capabilities.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from feature_pipeline.application.compile_plan import ShallowTaskInput, compile_run_plan
from feature_pipeline.application.profile_bridge import compiled_profile_from_core
from feature_pipeline.contracts import TaskSpec
from feature_pipeline.ports.adapters import AdapterCapabilities, AdapterRegistry
from pipeline_core.adapter_resolution import AdapterResolutionError, resolve_adapter
from pipeline_core.adapters import LaunchRequest, LaunchResult, build_claude_argv, build_codex_argv
from pipeline_core.execution import (
    ExecuteControls, ExecuteRequest, ExecutionError, _ensure_plan_compatible,
    _plan_fingerprint, execute_run,
)
from pipeline_core.project_profile import load_runnable_profile
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.state import Run
from pipeline_core.verification import VerifierAnchors, VerifierLaunchers
from tests.support.fakes import ScriptedExecutor, StubVerifier


class CapturingAdapter:
    """Retain every request, including continuations omitted by some shared doubles."""

    def __init__(self, delegate, *, name: str = "claude") -> None:
        self.delegate = delegate
        self.name = name
        self.requests: list[LaunchRequest] = []

    def launch(self, request: LaunchRequest) -> LaunchResult:
        self.requests.append(request)
        return self.delegate.launch(request)


class CodexFinalExecutor:
    def launch(self, request: LaunchRequest) -> LaunchResult:
        payload = {"role": "executor", "task_id": request.task_id, "attempt": 1,
                   "status": "implemented"}
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}},
            {"type": "turn.completed"},
        ]
        return LaunchResult(0, json.dumps(payload), "", "codex-thread",
                            "\n".join(json.dumps(event) for event in events))


def _spec(task_id: str = "BASE-1", *, depends_on: tuple[str, ...] = ()) -> TaskSpec:
    return TaskSpec.build(
        id=task_id, path=f"tasks/{task_id}.md", task_type="python", executor="python-executor",
        depends_on=depends_on, allowed_scope=("src/**",), max_repair_attempts=1,
    )


def _request(root: Path, executor: CapturingAdapter, *, specs=None, feature="baseline",
             task_verdicts=("PASS",), controls=None, environment=None) -> ExecuteRequest:
    prompt = root / f"{feature}.md"
    prompt.write_text("Baseline fixture prompt\n", encoding="utf-8")
    return ExecuteRequest(
        feature=feature, repo_root=root, run_dir=root / "runs" / feature,
        prompt_path=prompt, plan_path=None, specs=specs or (_spec(),), adapter=executor,
        launchers=VerifierLaunchers(
            task=CapturingAdapter(StubVerifier(task_verdicts), name=executor.name),
            test=CapturingAdapter(StubVerifier(("PASS",)), name=executor.name)),
        envelope_anchors=EnvelopeAnchors(project_root=".", agents_root="shared"),
        verifier_anchors=VerifierAnchors(project_root=str(root), agents_root="shared"),
        environment=environment if environment is not None else {"claude": True, "codex": True},
        controls=controls or ExecuteControls(plan_approved=True),
    )


class AdapterSelectionCharacterizationTests(unittest.TestCase):
    def test_auto_prefers_claude_and_explicit_unavailable_does_not_fallback(self) -> None:
        registry = AdapterRegistry(tuple(
            AdapterCapabilities(name, True, name == "claude", True, True, 3600.0)
            for name in ("claude", "codex")))
        self.assertEqual(registry.resolve(None).name, "claude")
        self.assertEqual(resolve_adapter(None, {"codex": True, "claude": True}).resolved, "claude")
        self.assertEqual(resolve_adapter("auto", {"codex": True}).resolved, "codex")
        self.assertEqual(resolve_adapter("codex", {"codex": True, "claude": True}).sourced, "explicit")
        for choice, environment in (("codex", {"claude": True}), ("auto", {}), ("unknown", {})):
            with self.subTest(choice=choice), self.assertRaises(AdapterResolutionError) as raised:
                resolve_adapter(choice, environment)
            self.assertEqual(raised.exception.code, "adapter-unavailable")

    def test_launch_contract_lowers_explicit_runtime_controls_and_codex_does_not_resume(self) -> None:
        self.assertTrue({"model", "effort"} <= {field.name for field in fields(LaunchRequest)})
        request = LaunchRequest(
            role="test_verifier", task_id="BASE-1", prompt="Return the verdict",
            report_path=Path("report.json"), read_only=True, no_tools=True,
            resume_session_id="previous-session", model="sonnet", effort="medium",
        )
        claude = build_claude_argv(request)
        self.assertEqual(claude[claude.index("--model") + 1], "sonnet")
        self.assertEqual(claude[claude.index("--effort") + 1], "medium")
        self.assertEqual(claude[claude.index("--resume") + 1], "previous-session")
        self.assertEqual(claude[claude.index("--tools") + 1], "")

        codex_request = replace(request, model="gpt-5.6-terra")
        codex = build_codex_argv(codex_request)
        self.assertEqual(codex[:2], ["codex", "exec"])
        self.assertEqual(codex[codex.index("--model") + 1], "gpt-5.6-terra")
        self.assertEqual(codex[codex.index("--config") + 1], 'model_reasoning_effort="medium"')
        self.assertEqual(codex[codex.index("--sandbox") + 1], "read-only")
        self.assertNotIn("resume", codex)

    def test_compiled_adapter_is_an_additional_resume_guard(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "task_model_routing" / "native.json"
        plan = compile_run_plan(feature="baseline", definitions=(ShallowTaskInput("BASE-1", "python"),),
                                profile=compiled_profile_from_core(load_runnable_profile(fixture)))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("fixture", encoding="utf-8")
            run = Run.create("baseline", prompt, None, root / "runs" / "baseline", root)
            run.set_control("plan_fingerprint", _plan_fingerprint(plan), sourced="explicit")
            _ensure_plan_compatible(run, plan)
            with self.assertRaises(ExecutionError) as raised:
                _ensure_plan_compatible(run, replace(plan, adapter="codex"))
            self.assertEqual(raised.exception.code, "plan-incompatible")
            self.assertIn("adapter", str(raised.exception))
            self.assertEqual(_plan_fingerprint(plan)["task.BASE-1.adapter"], "claude")


class DispatchAndResumeCharacterizationTests(unittest.TestCase):
    def test_executor_and_verifier_continuations_use_six_requests(self) -> None:
        with TemporaryDirectory() as directory:
            executor = CapturingAdapter(ScriptedExecutor())
            request = _request(Path(directory), executor)
            result = execute_run(request)
            self.assertTrue(result.ok, result.message)
            self.assertEqual(len(executor.requests), 2)
            first, envelope = executor.requests
            self.assertEqual(first.role, "python-executor")
            self.assertTrue(first.fresh_session)
            self.assertIsNone(first.resume_session_id)
            self.assertEqual(envelope.resume_session_id, "exec-sess")
            self.assertTrue(envelope.no_tools and envelope.read_only)
            for adapter, no_tools in ((request.launchers.task, False), (request.launchers.test, True)):
                self.assertEqual(len(adapter.requests), 2)
                report, verdict = adapter.requests
                self.assertTrue(report.fresh_session and report.read_only)
                self.assertIsNone(report.resume_session_id)
                self.assertEqual(report.no_tools, no_tools)
                self.assertTrue(verdict.no_tools and verdict.read_only)
                self.assertEqual(verdict.resume_session_id, "ver-sess")
            self.assertNotIn("# task_verifier\n", request.launchers.test.requests[0].prompt)
            self.assertIn("Stopped after stage 9", result.message)

    def test_codex_executor_final_result_removes_only_its_status_continuation(self) -> None:
        with TemporaryDirectory() as directory:
            executor = CapturingAdapter(CodexFinalExecutor(), name="codex")
            request = _request(Path(directory), executor, controls=ExecuteControls(
                plan_approved=True, adapter="codex", adapter_explicit=True))
            result = execute_run(request)
            self.assertTrue(result.ok, result.message)
            self.assertEqual(len(executor.requests), 1)
            self.assertEqual(len(request.launchers.task.requests), 2)
            self.assertEqual(len(request.launchers.test.requests), 2)
            envelope = result.run_dir / "reports" / "BASE-1" / "launch-1" / "executor-envelope-1.json"
            self.assertEqual(json.loads(envelope.read_text(encoding="utf-8"))["status"], "implemented")

    def test_resume_keeps_pin_and_skips_done_tasks_but_auto_drift_is_denied(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = execute_run(_request(root, CapturingAdapter(ScriptedExecutor())))
            self.assertTrue(first.ok, first.message)
            run = Run.load(first.run_dir, root)
            self.assertEqual(run.task("BASE-1").adapter, "claude")
            self.assertEqual(run.controls["adapter_resolved"]["value"], "claude")
            executor = CapturingAdapter(ScriptedExecutor())
            request = _request(root, executor, controls=ExecuteControls(plan_approved=True, resume=True))
            self.assertTrue(execute_run(request).ok)
            self.assertEqual(executor.requests, [])
            self.assertEqual(request.launchers.task.requests, [])
            self.assertEqual(request.launchers.test.requests, [])
            denied = execute_run(replace(request, environment={"codex": True}))
            self.assertEqual(denied.exit_code, 30)
            self.assertIn("adapter-switch", denied.message)
            self.assertEqual(executor.requests, [])
            self.assertEqual(Run.load(first.run_dir, root).task("BASE-1").adapter, "claude")

    def test_explicit_launch_failure_recovery_rejects_a_prior_executor_outcome(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executor = CapturingAdapter(ScriptedExecutor(("implemented", "launch-fail")))
            first = execute_run(_request(root, executor, task_verdicts=("FAIL",)))
            self.assertEqual(first.status, "retryable")
            run = Run.load(first.run_dir, root)
            self.assertEqual(run.task("BASE-1").attempts, 1)
            original = first.run_dir / "reports" / "BASE-1" / "launch-1" / "executor-1.md"
            original_bytes = original.read_bytes()
            resumed_executor = CapturingAdapter(ScriptedExecutor())
            resumed = execute_run(_request(root, resumed_executor, controls=ExecuteControls(
                plan_approved=True, resume=True, task="BASE-1",
                recovery_source_feature="baseline", recovery_task="BASE-1")))
            self.assertEqual(resumed.status, "error")
            self.assertIn("recovery-executor-outcome", resumed.message)
            run = Run.load(resumed.run_dir, root)
            self.assertEqual(run.task("BASE-1").attempts, 1)
            self.assertEqual(run.task("BASE-1").next_executor_launch_generation, 3)
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertEqual(resumed_executor.requests, [])

    def test_dependency_reuse_skips_all_launches_and_preserves_source_bytes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dependency, target = _spec(), _spec("BASE-2", depends_on=("BASE-1",))
            source = execute_run(_request(root, CapturingAdapter(ScriptedExecutor()),
                                          specs=(dependency,), feature="source"))
            self.assertTrue(source.ok, source.message)
            source_path = source.run_dir / "run.json"
            before = source_path.read_bytes()
            executor = CapturingAdapter(ScriptedExecutor())
            request = _request(root, executor, specs=(dependency, target), feature="consumer",
                               controls=ExecuteControls(plan_approved=True, task="BASE-2"))
            result = execute_run(request)
            self.assertTrue(result.ok, result.message)
            for adapter in (executor, request.launchers.task, request.launchers.test):
                self.assertEqual({item.task_id for item in adapter.requests}, {"BASE-2"})
            consumed = Run.load(result.run_dir, root).task("BASE-1")
            self.assertEqual(consumed.status, "done")
            self.assertTrue(consumed.reused_verification)
            self.assertEqual(source_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
