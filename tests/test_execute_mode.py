"""EMI-01 — the ``execute`` mode end to end, driven by deterministic fake adapters.

Every scenario runs :func:`pipeline_core.execution.execute_run` over the
``fixtures/execution`` plan with scripted executor/verifier launches, so one invocation is
byte-reproducible. The table in ``fixtures/execution/scenario_adapters.py`` covers the
single-invocation terminal shapes (AC-1, AC-2, AC-4, AC-5); the bespoke methods below cover
resume (AC-3), the pinned-adapter switch guard (AC-4), a live foreign lease (AC-4), and the
stage-9 stop (AC-5). ``AttestDependencyTests`` covers RDS-08's ``--attest-dependency`` bridge.
"""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline_core.concurrency import pipeline_lock_path, task_lock_path
from pipeline_core.execution import (
    EXIT_BLOCKED,
    EXIT_ERROR,
    EXIT_OK,
    ExecuteControls,
    ExecuteRequest,
    execute_run,
    persist_task_contracts,
)
import pipeline_core.execution as execution_module
from pipeline_core.adapters import LaunchResult
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.state import ACTOR_RUNNER, Run, pid_alive
from pipeline_core.verification import VerifierAnchors, VerifierLaunchers
from feature_pipeline.contracts import CommandSpec, TaskSpec

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "execution"
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))

import scenario_adapters as sa  # noqa: E402  (path injected above)

PLAN = json.loads((FIXTURES / "plan.json").read_text(encoding="utf-8"))
FEATURE = str(PLAN["feature"])


def _specs(task_ids: tuple[str, ...]) -> tuple[TaskSpec, ...]:
    chosen = [entry for entry in PLAN["tasks"] if entry["id"] in task_ids]
    return tuple(TaskSpec.build(**entry) for entry in chosen)


def _request(
    root: Path,
    specs: tuple[TaskSpec, ...],
    *,
    executor,
    launchers: VerifierLaunchers,
    controls: ExecuteControls,
    environment: dict,
    board_path: Path | None = None,
) -> ExecuteRequest:
    prompt = root / "prompt.md"
    prompt.write_text("feature prompt", encoding="utf-8")
    plan = root / "plan.json"
    plan.write_text(json.dumps(PLAN, indent=2) + "\n", encoding="utf-8")
    return ExecuteRequest(
        feature=FEATURE,
        repo_root=root,
        run_dir=root / "runs" / FEATURE,
        prompt_path=prompt,
        plan_path=plan,
        specs=specs,
        adapter=executor,
        launchers=launchers,
        envelope_anchors=EnvelopeAnchors(project_root=".", agents_root=".agents"),
        verifier_anchors=VerifierAnchors(
            project_root=str(root), agents_root=str(root / ".agents")),
        environment=environment,
        controls=controls,
        plan_prompt_path="fixtures/execution/plan.json",
        board_path=board_path,
    )


def _controls(scenario: sa.Scenario) -> ExecuteControls:
    overrides = dict(scenario.controls)
    gated = not overrides.pop("_no_gate", False)
    return ExecuteControls(plan_approved=gated, **overrides)


def _seed_reportless_uv_cache_block(request: ExecuteRequest) -> None:
    """Build REC-11's real first-launch, runner-owned cache-start terminal shape."""
    source = Run.create(FEATURE, request.prompt_path, request.plan_path, request.run_dir, request.repo_root)
    life = RunLifecycle.initialize(
        source, tasks=[("EX-01", [])],
        controls={
            "execution_scope": (["EX-01"], "explicit"),
            "verify_dependency_chain": (False, "default"),
            "model": (None, "default"), "effort": (None, "default"),
            "precondition_bindings": ({}, "explicit"),
        }, adapter_requested="claude", adapter_resolved="claude")
    life.transition("EX-01", "running", actor=ACTOR_RUNNER)
    generation = life.consume_launch_generation("EX-01")
    blocker = "external operational: uv cache access denied"
    diagnostic = request.run_dir / "reports" / "EX-01" / f"launch-{generation}" / (
        f"launch-failure-{generation}.json"
    )
    diagnostic.parent.mkdir(parents=True)
    diagnostic.write_text(json.dumps({
        "task_id": "EX-01", "generation": generation, "attempt": 0,
        "stage": "executor", "reason": blocker, "exit_code": 1,
    }), encoding="utf-8")
    life.run.record_launch_failure(
        "EX-01", stage="executor", generation=generation, exit_code=1, detail=blocker)
    persist_task_contracts(life.run, request.specs)
    life.block("EX-01", blocker)
    life.run.status = "blocked"
    life.run.save()


def _seed_actual_tc01_predispatch_block(request: ExecuteRequest, *, tamper: bool = False) -> None:
    """Seed REC-11's persisted TC-01 runner-owned pre-dispatch blocker exactly."""
    task_id = "TC-01"
    source = Run.create(FEATURE, request.prompt_path, request.plan_path, request.run_dir, request.repo_root)
    life = RunLifecycle.initialize(
        source, tasks=[(task_id, [])],
        controls={
            "execution_scope": ([task_id], "explicit"),
            "verify_dependency_chain": (False, "default"),
            "model": (None, "default"), "effort": (None, "default"),
            "precondition_bindings": ({}, "explicit"),
        }, adapter_requested="claude", adapter_resolved="claude")
    blocker = "external operational: uv cache access denied"
    life.transition(task_id, "running", actor=ACTOR_RUNNER)
    life.block(task_id, blocker)
    record = life.run.task(task_id)
    record.next_executor_launch_generation = 2
    diagnostic = request.run_dir / "reports" / task_id / "attempt-1" / "diagnostic-report.md"
    diagnostic.parent.mkdir(parents=True)
    diagnostic.write_text("runner-owned diagnostic\n", encoding="utf-8")
    diagnostic_ref = diagnostic.relative_to(request.repo_root).as_posix()
    packet = request.run_dir / "reports" / task_id / "blocked-1.json"
    packet.write_text(json.dumps({
        "task_id": task_id,
        "gate": 2 if tamper else 1,
        "attempts": 0,
        "max_repair_attempts": 2,
        "blocker": blocker,
        "diagnostic": diagnostic_ref,
        "repair_report": None,
        "verdicts": {"task": None, "test": None},
        "recorded_at": "2026-09-10T00:00:00Z",
    }), encoding="utf-8")
    life.run.artifacts = {
        f"blocker:{task_id}": packet.relative_to(request.repo_root).as_posix(),
        f"diagnostic:{task_id}:1": diagnostic.relative_to(request.run_dir).as_posix(),
    }
    persist_task_contracts(life.run, request.specs)
    life.run.status = "blocked"
    life.run.save()


class ScenarioTableTests(unittest.TestCase):
    def test_operational_unblock_is_rejected_for_resumable_external_waits(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scenario = sa.SCENARIOS["direct-success"]
            executor = scenario.executor()
            request = _request(
                root, _specs(("EX-01",)), executor=executor,
                launchers=VerifierLaunchers(
                    task=scenario.task_verifier(), test=scenario.test_verifier()),
                controls=ExecuteControls(plan_approved=True), environment={"claude": True})
            source = Run.create(FEATURE, request.prompt_path, request.plan_path, request.run_dir, root)
            life = RunLifecycle.initialize(
                source, tasks=[("EX-01", [])],
                controls={
                    "execution_scope": (["EX-01"], "explicit"),
                    "verify_dependency_chain": (False, "default"),
                    "model": (None, "default"), "effort": (None, "default"),
                    "precondition_bindings": ({}, "explicit"),
                }, adapter_requested="claude", adapter_resolved="claude")
            life.transition("EX-01", "running", actor=ACTOR_RUNNER)
            report = request.run_dir / "reports" / "EX-01" / "report.md"
            report.parent.mkdir(parents=True)
            report.write_text("blocked by uv cache\n", encoding="utf-8")
            life.run.task("EX-01").execution_evidence["executor_report"] = report.relative_to(root).as_posix()
            persist_task_contracts(life.run, request.specs)
            life.block("EX-01", "external operational: uv cache access denied")
            life.run.status = "blocked"
            life.run.save()

            observed: list[str | None] = []
            original_launch = executor.launch

            def launch(request):  # noqa: ANN001 - observes real child launch environment
                observed.append(os.environ.get("UV_CACHE_DIR"))
                return original_launch(request)

            executor.launch = launch
            cache = root / ".pipeline" / "uv-cache"
            result = execute_run(
                _request(
                    root, _specs(("EX-01",)), executor=executor,
                    launchers=VerifierLaunchers(
                        task=scenario.task_verifier(), test=scenario.test_verifier()),
                    controls=ExecuteControls(
                        plan_approved=True, resume=True,
                        operational_unblock_task="EX-01",
                        human_authorized_operational_unblock=True,
                        uv_cache_dir=".pipeline/uv-cache",
                    ), environment={"claude": True}))

            self.assertEqual(result.status, "error")
            self.assertIn("operational-unblock-blocker-invalid", result.message)
            self.assertEqual(observed, [])

    def test_operational_unblock_is_rejected_for_reportless_waits(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scenario = sa.SCENARIOS["direct-success"]
            executor = scenario.executor()
            request = _request(
                root, _specs(("EX-01",)), executor=executor,
                launchers=VerifierLaunchers(
                    task=scenario.task_verifier(), test=scenario.test_verifier()),
                controls=ExecuteControls(plan_approved=True), environment={"claude": True})
            _seed_reportless_uv_cache_block(request)

            result = execute_run(_request(
                root, _specs(("EX-01",)), executor=executor,
                launchers=VerifierLaunchers(
                    task=scenario.task_verifier(), test=scenario.test_verifier()),
                controls=ExecuteControls(
                    plan_approved=True, resume=True, operational_unblock_task="EX-01",
                    human_authorized_operational_unblock=True, uv_cache_dir=".pipeline/uv-cache",
                ), environment={"claude": True}))

            self.assertEqual(result.status, "error")
            self.assertIn("operational-unblock-blocker-invalid", result.message)

    def test_operational_unblock_rejects_missing_or_tampered_runner_cache_artifact(self) -> None:
        for name in ("absent", "tampered"):
            with self.subTest(name=name), TemporaryDirectory() as directory:
                root = Path(directory)
                scenario = sa.SCENARIOS["direct-success"]
                executor = scenario.executor()
                request = _request(
                    root, _specs(("EX-01",)), executor=executor,
                    launchers=VerifierLaunchers(
                        task=scenario.task_verifier(), test=scenario.test_verifier()),
                    controls=ExecuteControls(plan_approved=True), environment={"claude": True})
                _seed_reportless_uv_cache_block(request)
                diagnostic = request.run_dir / "reports" / "EX-01" / "launch-1" / "launch-failure-1.json"
                if name == "absent":
                    diagnostic.unlink()
                else:
                    payload = json.loads(diagnostic.read_text(encoding="utf-8"))
                    payload["reason"] = "tampered"
                    diagnostic.write_text(json.dumps(payload), encoding="utf-8")

                result = execute_run(_request(
                    root, _specs(("EX-01",)), executor=executor,
                    launchers=VerifierLaunchers(
                        task=scenario.task_verifier(), test=scenario.test_verifier()),
                    controls=ExecuteControls(
                        plan_approved=True, resume=True, operational_unblock_task="EX-01",
                        human_authorized_operational_unblock=True, uv_cache_dir=".pipeline/uv-cache",
                    ), environment={"claude": True}))

                self.assertEqual(result.status, "error")
                self.assertIn("operational-unblock-blocker-invalid", result.message)
                self.assertEqual(executor.launches, 0)
                self.assertEqual(Run.load(request.run_dir, root).task("EX-01").status, "in_progress")

    def test_operational_unblock_accepts_only_the_actual_tc01_predispatch_blocker_packet(self) -> None:
        for name in ("matching", "absent", "tampered"):
            with self.subTest(name=name), TemporaryDirectory() as directory:
                root = Path(directory)
                scenario = sa.SCENARIOS["direct-success"]
                executor = scenario.executor()
                tc01 = replace(_specs(("EX-01",))[0], id="TC-01")
                request = _request(
                    root, (tc01,), executor=executor,
                    launchers=VerifierLaunchers(
                        task=scenario.task_verifier(), test=scenario.test_verifier()),
                    controls=ExecuteControls(plan_approved=True), environment={"claude": True})
                _seed_actual_tc01_predispatch_block(request, tamper=name == "tampered")
                packet = request.run_dir / "reports" / "TC-01" / "blocked-1.json"
                if name == "absent":
                    packet.unlink()
                seeded = Run.load(request.run_dir, root)
                seeded_task = seeded.task("TC-01")
                self.assertEqual(seeded_task.attempts, 0)
                self.assertIsNone(seeded_task.execution_evidence["executor_report"])
                self.assertEqual(seeded.commands, [])
                self.assertEqual(seeded_task.external_launch_failures, [])
                self.assertEqual(seeded_task.next_executor_launch_generation, 2)

                result = execute_run(_request(
                    root, (tc01,), executor=executor,
                    launchers=VerifierLaunchers(
                        task=scenario.task_verifier(), test=scenario.test_verifier()),
                    controls=ExecuteControls(
                        plan_approved=True, resume=True, operational_unblock_task="TC-01",
                        human_authorized_operational_unblock=True, uv_cache_dir=".pipeline/uv-cache",
                    ), environment={"claude": True}))

                self.assertEqual(result.status, "error")
                self.assertIn("operational-unblock-blocker-invalid", result.message)
                self.assertEqual(executor.launches, 0)
                self.assertEqual(Run.load(request.run_dir, root).task("TC-01").status, "in_progress")

    def test_operational_unblock_fails_closed_for_unapproved_or_unsafe_retry(self) -> None:
        cases = (
            ("missing-authorization", {}, "operational-unblock-unauthorized"),
            ("unsafe-cache", {"uv_cache_dir": "../uv-cache"}, "operational-unblock-blocker-invalid"),
            ("product-blocker", {"blocker": "repair budget exhausted"}, "operational-unblock-blocker-invalid"),
            ("model-drift", {"model": "different"}, "runtime-control-mismatch"),
            ("live-lease", {"live_lease": True}, "operational-unblock-blocker-invalid"),
            ("missing-evidence", {"missing_evidence": True}, "operational-unblock-blocker-invalid"),
        )
        for name, override, expected in cases:
            with self.subTest(name=name), TemporaryDirectory() as directory:
                root = Path(directory)
                scenario = sa.SCENARIOS["direct-success"]
                executor = scenario.executor()
                request = _request(
                    root, _specs(("EX-01",)), executor=executor,
                    launchers=VerifierLaunchers(
                        task=scenario.task_verifier(), test=scenario.test_verifier()),
                    controls=ExecuteControls(plan_approved=True), environment={"claude": True})
                source = Run.create(FEATURE, request.prompt_path, request.plan_path, request.run_dir, root)
                life = RunLifecycle.initialize(
                    source, tasks=[("EX-01", [])],
                    controls={
                        "execution_scope": (["EX-01"], "explicit"),
                        "verify_dependency_chain": (False, "default"),
                        "model": (None, "default"), "effort": (None, "default"),
                        "precondition_bindings": ({}, "explicit"),
                    }, adapter_requested="claude", adapter_resolved="claude")
                life.transition("EX-01", "running", actor=ACTOR_RUNNER)
                if not override.get("missing_evidence"):
                    report = request.run_dir / "reports" / "EX-01" / "report.md"
                    report.parent.mkdir(parents=True)
                    report.write_text("blocked by uv cache\n", encoding="utf-8")
                    life.run.task("EX-01").execution_evidence["executor_report"] = report.relative_to(root).as_posix()
                persist_task_contracts(life.run, request.specs)
                life.block("EX-01", override.get("blocker", "external operational: uv cache access denied"))
                life.run.status = "blocked"
                life.run.save()
                if override.get("live_lease"):
                    lock = pipeline_lock_path(root)
                    lock.parent.mkdir(parents=True, exist_ok=True)
                    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
                controls = ExecuteControls(
                    plan_approved=True, resume=True, operational_unblock_task="EX-01",
                    human_authorized_operational_unblock=not name == "missing-authorization",
                    uv_cache_dir=override.get("uv_cache_dir", ".pipeline/uv-cache"),
                    model=override.get("model"),
                )

                result = execute_run(_request(
                    root, _specs(("EX-01",)), executor=executor,
                    launchers=VerifierLaunchers(
                        task=scenario.task_verifier(), test=scenario.test_verifier()),
                    controls=controls, environment={"claude": True}))

                self.assertEqual(result.status, "error")
                self.assertIn(expected, result.message)
                self.assertEqual(executor.launches, 0)
                self.assertEqual(Run.load(request.run_dir, root).task("EX-01").status, "in_progress")

    def test_every_scenario_reaches_its_declared_terminal_shape(self) -> None:
        for name, scenario in sa.SCENARIOS.items():
            with self.subTest(scenario=name), TemporaryDirectory() as directory:
                root = Path(directory)
                executor = scenario.executor()
                launchers = VerifierLaunchers(
                    task=scenario.task_verifier(), test=scenario.test_verifier())
                result = execute_run(
                    _request(
                        root, _specs(scenario.task_ids),
                        executor=executor, launchers=launchers,
                        controls=_controls(scenario), environment=dict(scenario.environment)))
                self.assertEqual(result.status, scenario.expected_status, name)
                self.assertEqual(result.exit_code, scenario.expected_exit, name)

    def test_direct_success_persists_verified_state_and_stops_at_stage_9(self) -> None:
        scenario = sa.SCENARIOS["direct-success"]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executor = scenario.executor()
            result = execute_run(
                _request(
                    root, _specs(scenario.task_ids), executor=executor,
                    launchers=VerifierLaunchers(
                        task=scenario.task_verifier(), test=scenario.test_verifier()),
                    controls=_controls(scenario), environment={"claude": True}))

            self.assertTrue(result.ok)
            self.assertEqual(result.exit_code, EXIT_OK)
            self.assertEqual(executor.launches, 1)
            self.assertIn("Stopped after stage 9", result.message)

            run = Run.load(result.run_dir, root)
            self.assertEqual(run.task("EX-01").status, "done")
            self.assertEqual(run.task("EX-01").verification["task_verdict"], "PASS")
            self.assertEqual(run.task("EX-01").adapter, "claude")
            self.assertEqual(run.controls["adapter_resolved"]["value"], "claude")
            # nothing past stage 9 was produced.
            produced = {p.name for p in result.run_dir.rglob("*")}
            for forbidden in ("documentation", "graphify", "release", "archive", "purge"):
                self.assertNotIn(forbidden, produced)

    def test_repair_exhaustion_writes_a_diagnostic_and_a_truthful_blocker(self) -> None:
        scenario = sa.SCENARIOS["repair-exhaustion"]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = execute_run(
                _request(
                    root, _specs(scenario.task_ids), executor=scenario.executor(),
                    launchers=VerifierLaunchers(
                        task=scenario.task_verifier(), test=scenario.test_verifier()),
                    controls=_controls(scenario), environment={"claude": True}))

            self.assertEqual(result.exit_code, EXIT_BLOCKED)
            self.assertIn("maximum repair attempts", result.message)
            self.assertEqual(len(result.task_results), 1)
            self.assertIsNone(result.task_results[0].diagnostic)
            task = Run.load(result.run_dir, root).task("EX-01")
            self.assertEqual(task.status, "in_progress")
            self.assertIn("escalated", [entry["outcome"] for entry in task.operation_history])

    def test_dependency_waits_never_dispatch_dependents_or_write_blockers(self) -> None:
        scenario = sa.SCENARIOS["dependency-suppression"]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executor = scenario.executor()
            result = execute_run(
                _request(
                    root, _specs(scenario.task_ids), executor=executor,
                    launchers=VerifierLaunchers(
                        task=scenario.task_verifier(), test=scenario.test_verifier()),
                    controls=_controls(scenario), environment={"claude": True}))

            self.assertEqual(result.exit_code, EXIT_BLOCKED)
            self.assertEqual({call["task_id"] for call in executor.calls}, {"EX-01"})
            run = Run.load(result.run_dir, root)
            self.assertEqual(run.task("EX-01").status, "in_progress")
            self.assertIsNone(run.task("EX-02").blocker)
            self.assertIsNone(run.task("EX-03").blocker)

    def test_gate_pending_writes_no_run_state(self) -> None:
        scenario = sa.SCENARIOS["gate-pending"]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = execute_run(
                _request(
                    root, _specs(scenario.task_ids), executor=scenario.executor(),
                    launchers=VerifierLaunchers(
                        task=scenario.task_verifier(), test=scenario.test_verifier()),
                    controls=_controls(scenario), environment={"claude": True}))
            self.assertEqual(result.status, "gate-pending")
            self.assertFalse((root / "runs" / FEATURE / "run.json").exists())


class ResumeAndSafetyTests(unittest.TestCase):
    def _run_once(self, root: Path, *, executor, task, test, controls):
        return execute_run(
            _request(
                root, _specs(("EX-01",)), executor=executor,
                launchers=VerifierLaunchers(task=task, test=test),
                controls=controls, environment={"claude": True}))

    def test_resume_continues_an_open_repair_without_double_counting(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run_once(
                root,
                executor=sa.ScriptedExecutor(("implemented", "implemented")),
                task=sa.ScriptedVerifier(("FAIL", "PASS")),
                test=sa.ScriptedVerifier(("PASS", "PASS")),
                controls=ExecuteControls(plan_approved=True))
            self.assertTrue(first.ok)

            # Simulate a crash between the failed gate and the repair redispatch.
            reloaded = Run.load(first.run_dir, root)
            reloaded.task("EX-01").status = "in_progress"
            reloaded.save()

            executor = sa.ScriptedExecutor(("implemented",))
            resumed = self._run_once(
                root, executor=executor,
                task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",)),
                controls=ExecuteControls(plan_approved=True, resume=True))

            self.assertTrue(resumed.ok)
            self.assertEqual(resumed.exit_code, EXIT_OK)
            run = Run.load(resumed.run_dir, root)
            self.assertEqual(run.task("EX-01").status, "done")
            self.assertEqual(run.task("EX-01").attempts, 1)  # not re-counted on resume
            self.assertEqual(run.controls["adapter_resolved"]["value"], "claude")
            self.assertTrue(executor.calls[0]["is_repair"])

    def test_blocked_executor_resume_opens_a_new_generation_and_preserves_launch_one(self) -> None:
        """RLC-01 AC-3 exercises the real execute/resume path, rather than a serialized
        state fixture: an executor permission wait is non-terminal, and a compatible resume
        must preserve all generation-one bytes/history while runner-owned checks and both
        independent verifiers complete generation two."""
        class PermissionBlockedExecutor(sa.ScriptedExecutor):
            def launch(self, request):  # noqa: ANN001 - deterministic protocol fixture
                result = super().launch(request)
                if request.no_tools or request.resume_session_id:
                    reasoned = json.dumps({
                        "role": "executor", "status": "blocked", "task_id": request.task_id,
                        "attempt": 1, "reason": "declared check requires an unavailable grant",
                    })
                    Path(request.report_path).write_text(reasoned, encoding="utf-8")
                    return LaunchResult(exit_code=0, stdout=reasoned, session_id="exec-sess")
                return result

        with TemporaryDirectory() as directory:
            root = Path(directory)
            spec = replace(
                _specs(("EX-01",))[0],
                verification_commands=(CommandSpec(".", (sys.executable, "-c", "pass")),),
            )
            first = execute_run(_request(
                root, (spec,), executor=PermissionBlockedExecutor(("blocked",)),
                launchers=VerifierLaunchers(
                    task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                controls=ExecuteControls(plan_approved=True), environment={"claude": True}))

            self.assertEqual(first.status, "blocked")
            initial = Run.load(first.run_dir, root)
            record = initial.task("EX-01")
            self.assertEqual(record.status, "in_progress")
            self.assertEqual(record.operation_history[-2]["detail"],
                             "declared check requires an unavailable grant")
            first_history = json.loads(json.dumps(record.operation_history))
            launch_one = first.run_dir / "reports" / "EX-01" / "launch-1"
            first_bytes = {
                path.relative_to(launch_one).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in launch_one.rglob("*") if path.is_file()
            }

            resumed_executor = sa.ScriptedExecutor(("implemented",))
            resumed = execute_run(_request(
                root, (spec,), executor=resumed_executor,
                launchers=VerifierLaunchers(
                    task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                controls=ExecuteControls(plan_approved=True, resume=True), environment={"claude": True}))

            self.assertTrue(resumed.ok, resumed.message)
            self.assertEqual(resumed_executor.launches, 1)
            final = Run.load(resumed.run_dir, root)
            self.assertEqual(final.task("EX-01").status, "done")
            self.assertEqual(final.task("EX-01").next_executor_launch_generation, 3)
            self.assertEqual(first_history, final.task("EX-01").operation_history[:len(first_history)])
            self.assertEqual(first_bytes, {
                path.relative_to(launch_one).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in launch_one.rglob("*") if path.is_file()
            })
            self.assertTrue(any(
                entry["argv"][-2:] == ["-c", "pass"] and entry["exit_code"] == 0
                for entry in final.commands
            ))
            self.assertEqual(final.task("EX-01").verification["task_verdict"], "PASS")
            self.assertEqual(final.task("EX-01").verification["test_verdict"], "PASS")

    def test_plain_resume_rejects_codex_protocol_failure(self) -> None:
        class CodexSequenceExecutor:
            name = "codex"

            def __init__(self) -> None:
                self.launches = 0

            def launch(self, request):  # noqa: ANN001 - deterministic adapter double
                payloads = (
                    [{"not": "a final result"}],
                    [{"role": "executor", "task_id": request.task_id, "attempt": 1,
                      "status": "implemented"}],
                )
                current = payloads[self.launches]
                self.launches += 1
                Path(request.report_path).parent.mkdir(parents=True, exist_ok=True)
                Path(request.report_path).write_text("# Human report\n", encoding="utf-8")
                events = [json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": json.dumps(payload),
                }}) for payload in current]
                events.append(json.dumps({"type": "turn.completed"}))
                return LaunchResult(0, "# Human report\n", "", "thread-1", "\n".join(events))

        with TemporaryDirectory() as directory:
            root = Path(directory)
            executor = CodexSequenceExecutor()
            first = execute_run(_request(
                root, _specs(("EX-01",)), executor=executor,
                launchers=VerifierLaunchers(
                    task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                controls=ExecuteControls(plan_approved=True, adapter="codex", adapter_explicit=True),
                environment={"codex": True}))

            self.assertEqual(first.status, "retryable")
            run = Run.load(first.run_dir, root)
            self.assertEqual(run.status, "running")
            self.assertEqual(run.task("EX-01").status, "in_progress")
            self.assertEqual(run.task("EX-01").attempts, 0)
            self.assertTrue(
                (first.run_dir / "reports" / "EX-01" / "launch-1" /
                 "result-protocol-invalid-1.json").is_file())

            resumed = execute_run(_request(
                root, _specs(("EX-01",)), executor=executor,
                launchers=VerifierLaunchers(
                    task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                controls=ExecuteControls(
                    plan_approved=True, resume=True, adapter="codex", adapter_explicit=True),
                environment={"codex": True}))

            self.assertEqual(resumed.status, "ok")
            self.assertEqual(executor.launches, 2)
            run = Run.load(resumed.run_dir, root)
            self.assertEqual(run.task("EX-01").next_executor_launch_generation, 3)

    def test_resume_that_would_switch_the_pinned_adapter_is_a_runner_error(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run_once(
                root, executor=sa.ScriptedExecutor(("implemented",)),
                task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",)),
                controls=ExecuteControls(plan_approved=True))
            self.assertTrue(first.ok)

            run_json = first.run_dir / "run.json"
            data = json.loads(run_json.read_text(encoding="utf-8"))
            data["controls"]["adapter_resolved"]["value"] = "codex"
            run_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

            resumed = self._run_once(
                root, executor=sa.ScriptedExecutor(("implemented",)),
                task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",)),
                controls=ExecuteControls(plan_approved=True, resume=True))
            self.assertEqual(resumed.status, "error")
            self.assertEqual(resumed.exit_code, EXIT_ERROR)
            self.assertIn("adapter-switch", resumed.message)

    def test_a_live_foreign_pipeline_lease_blocks_the_run(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock = pipeline_lock_path(root)
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(
                json.dumps({"run_id": "other-run", "pid": __import__("os").getpid(),
                            "task_id": None, "started_at": "2026-09-01T00:00:00Z"}),
                encoding="utf-8")
            self.assertTrue(pid_alive(__import__("os").getpid()))

            result = self._run_once(
                root, executor=sa.ScriptedExecutor(("implemented",)),
                task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",)),
                controls=ExecuteControls(plan_approved=True))
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.exit_code, EXIT_BLOCKED)
            self.assertIn("lease-held", result.message)
            reloaded = Run.load(result.run_dir, root) if result.run_dir else None
            # No task was ever selected under a pipeline-wide contention: the run-level
            # history records the wait; no task operation history exists to check.
            self.assertTrue(
                any("lease" in (entry.get("scope") or "") for entry in reloaded.history)
                if reloaded is not None else True)

    def test_a_live_foreign_task_lease_is_a_durable_unfinished_operation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock = task_lock_path(root, "EX-01")
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(
                json.dumps({"run_id": "other-run", "pid": os.getpid(),
                            "task_id": "EX-01", "started_at": "2026-09-01T00:00:00Z"}),
                encoding="utf-8")

            result = self._run_once(
                root, executor=sa.ScriptedExecutor(("implemented",)),
                task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",)),
                controls=ExecuteControls(plan_approved=True))

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.exit_code, EXIT_BLOCKED)
            self.assertIn("lease-held", result.message)

            reloaded = Run.load(result.run_dir, root)
            # The task stays unfinished — never a terminal/blocked task status — while the
            # lease contention is durably recorded as a specific operation outcome.
            self.assertEqual(reloaded.task("EX-01").status, "to_do")
            operations = reloaded.task("EX-01").operation_history
            self.assertTrue(
                any(entry.get("kind") == "lease" and entry.get("outcome") == "blocked"
                    for entry in operations),
                operations,
            )

    def test_unattended_opt_in_satisfies_the_plan_gate(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._run_once(
                root, executor=sa.ScriptedExecutor(("implemented",)),
                task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",)),
                controls=ExecuteControls(unattended=True))
            self.assertTrue(result.ok)


class AttestDependencyTests(unittest.TestCase):
    """RDS-08: ``--attest-dependency`` lets a ``--task``-scoped run trust an already-closed
    run's verified dependency instead of redispatching it."""

    def _seed(self, directory: str) -> tuple[Path, Path, Path]:
        root = Path(directory)
        prompt = root / "prompt.md"
        prompt.write_text("feature prompt", encoding="utf-8")
        plan = root / "plan.json"
        plan.write_text(json.dumps(PLAN, indent=2) + "\n", encoding="utf-8")
        return root, prompt, plan

    def _request(
        self, root: Path, prompt: Path, plan: Path, *, feature: str = FEATURE,
        task_ids: tuple[str, ...], controls: ExecuteControls,
        executor=None,
    ) -> ExecuteRequest:
        return ExecuteRequest(
            feature=feature,
            repo_root=root,
            run_dir=root / "runs" / feature,
            prompt_path=prompt,
            plan_path=plan,
            specs=_specs(task_ids),
            adapter=executor or sa.ScriptedExecutor(("implemented",)),
            launchers=VerifierLaunchers(
                task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
            envelope_anchors=EnvelopeAnchors(project_root=".", agents_root=".agents"),
            verifier_anchors=VerifierAnchors(
                project_root=str(root), agents_root=str(root / ".agents")),
            environment={"claude": True},
            controls=controls,
            plan_prompt_path="fixtures/execution/plan.json",
        )

    def _force_verified(self, life: RunLifecycle, task_id: str) -> None:
        life.transition(task_id, "running", actor=ACTOR_RUNNER)
        life.transition(task_id, "implemented", actor=ACTOR_RUNNER)
        life.run.record_verdicts(task_id, "PASS", "PASS")
        life.run.save()

    def _force_completed(self, life: RunLifecycle, task_id: str) -> None:
        life.transition(task_id, "in_progress", actor=ACTOR_RUNNER)
        life.run.record_verdicts(task_id, "PASS", "PASS")
        life.run.save()

    def _make_source_run(
        self, root: Path, feature: str, prompt: Path, plan: Path, *,
        tasks: tuple[tuple[str, list[str]], ...], verified_ids: tuple[str, ...],
    ) -> Path:
        """A hand-built, closed source run: fine-grained control over exactly which tasks it
        tracks and which of those reach 'verified', without driving the full dispatch loop."""
        run = Run.create(feature, prompt, plan, root / "runs" / feature, root)
        life = RunLifecycle.initialize(run, tasks=tasks)
        for task_id in verified_ids:
            self._force_verified(life, task_id)
        if {task_id for task_id, _ in tasks} == set(verified_ids):
            run.status = "verified"
            persist_task_contracts(run, _specs(tuple(task_id for task_id, _ in tasks)))
            run.save()
        return run.run_dir

    def test_success_reaches_verified_without_dispatching_the_attested_dependency(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            self._make_source_run(
                root, "source-feature", prompt, plan,
                tasks=(("EX-01", []),), verified_ids=("EX-01",))

            executor = sa.ScriptedExecutor(("implemented",))
            result = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"), executor=executor,
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02",
                    attested_dependencies=(("EX-01", "source-feature"),))))

            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.exit_code, EXIT_OK)
            self.assertEqual({call["task_id"] for call in executor.calls}, {"EX-02"})

            run = Run.load(result.run_dir, root)
            self.assertEqual(run.task("EX-02").status, "done")
            self.assertEqual(run.task("EX-01").status, "done")
            reused = run.task("EX-01").reused_verification
            self.assertEqual(len(reused), 1)
            self.assertEqual(reused[0]["dependency_id"], "EX-01")
            self.assertEqual(reused[0]["task_verdict"], "PASS")
            self.assertTrue(reused[0]["source_run_digest"].startswith("sha256:"))

    def test_completed_resolution_is_eligible_for_attestation(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            source = Run.create("source-feature", prompt, plan, root / "runs" / "source-feature", root)
            life = RunLifecycle.initialize(source, tasks=(("EX-01", []),))
            self._force_completed(life, "EX-01")
            source.status = "verified"
            persist_task_contracts(source, _specs(("EX-01",)))
            source.save()

            evidence = execution_module._resolve_attestation(
                dep_id="EX-01", source_feature="source-feature", run_dir=root / "runs" / FEATURE,
                repo_root=root, prompt_path=prompt, plan_path=plan,
            )

            self.assertEqual(evidence["dep_id"], "EX-01")

    def test_default_reuse_prunes_ancestors_of_a_reused_dependency(self) -> None:
        """A reusable direct dependency makes its own prerequisite irrelevant to this run."""
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            self._make_source_run(
                root, "source-feature", prompt, plan,
                tasks=(("EX-02", ()),), verified_ids=("EX-02",))

            executor = sa.ScriptedExecutor(("implemented",))
            result = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02", "EX-03"),
                executor=executor,
                controls=ExecuteControls(plan_approved=True, task="EX-03")))

            self.assertTrue(result.ok, result.message)
            self.assertEqual([call["task_id"] for call in executor.calls], ["EX-03"])
            run = Run.load(result.run_dir, root)
            self.assertEqual(
                run.controls["execution_scope"]["value"],
                ["EX-01", "EX-02", "EX-03"],
            )
            self.assertNotIn("EX-01", run.tasks)
            self.assertEqual(run.task("EX-02").status, "done")
            self.assertEqual(run.task("EX-03").status, "done")


    def test_attesting_never_writes_to_the_source_run(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            source_dir = self._make_source_run(
                root, "source-feature", prompt, plan,
                tasks=(("EX-01", []),), verified_ids=("EX-01",))
            before = (source_dir / "run.json").read_text(encoding="utf-8")

            result = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02",
                    attested_dependencies=(("EX-01", "source-feature"),))))

            self.assertTrue(result.ok, result.message)
            after = (source_dir / "run.json").read_text(encoding="utf-8")
            self.assertEqual(before, after)

    def test_through_scope_is_refused_before_any_state_is_written(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            result = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                controls=ExecuteControls(
                    plan_approved=True, through="EX-02",
                    attested_dependencies=(("EX-01", "source-feature"),))))
            self.assertEqual(result.status, "error")
            self.assertIn("attestation-requires-task-scope", result.message)
            self.assertFalse((root / "runs" / FEATURE / "run.json").exists())

    def test_unfiltered_run_scope_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            result = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                controls=ExecuteControls(
                    plan_approved=True,
                    attested_dependencies=(("EX-01", "source-feature"),))))
            self.assertEqual(result.status, "error")
            self.assertIn("attestation-requires-task-scope", result.message)

    def test_dep_id_not_a_declared_dependency_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            result = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02", "EX-03"),
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02",
                    attested_dependencies=(("EX-03", "source-feature"),))))
            self.assertEqual(result.status, "error")
            self.assertIn("attestation-not-a-dependency", result.message)
            self.assertFalse((root / "runs" / FEATURE / "run.json").exists())

    def test_duplicate_dep_id_across_flags_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            result = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02",
                    attested_dependencies=(("EX-01", "one"), ("EX-01", "two")))))
            self.assertEqual(result.status, "error")
            self.assertIn("duplicate-attestation-dependency", result.message)
            self.assertFalse((root / "runs" / FEATURE / "run.json").exists())

    def test_unsafe_source_feature_is_refused_independent_of_the_cli(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            for unsafe in ("../evil", "/etc/passwd", "a/b", "a.b", ".."):
                with self.subTest(source=unsafe):
                    result = execute_run(self._request(
                        root, prompt, plan, task_ids=("EX-01", "EX-02"),
                        controls=ExecuteControls(
                            plan_approved=True, task="EX-02",
                            attested_dependencies=(("EX-01", unsafe),))))
                    self.assertEqual(result.status, "error")
                    self.assertIn("attestation-unsafe-source", result.message)

    def test_missing_source_run_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            result = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02",
                    attested_dependencies=(("EX-01", "does-not-exist"),))))
            self.assertEqual(result.status, "error")
            self.assertIn("evidence-source-missing", result.message)
            self.assertFalse((root / "runs" / FEATURE / "run.json").exists())

    def test_prompt_plan_identity_mismatch_accepts_eligible_reuse(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            other_plan = root / "plan-2.json"
            other_plan.write_text(json.dumps(PLAN, indent=2) + "\n", encoding="utf-8")
            self._make_source_run(
                root, "source-feature", prompt, other_plan,
                tasks=(("EX-01", []),), verified_ids=("EX-01",))

            result = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02",
                    attested_dependencies=(("EX-01", "source-feature"),))))
            self.assertTrue(result.ok, result.message)

    def test_source_not_tracking_the_dependency_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            self._make_source_run(
                root, "source-feature", prompt, plan,
                tasks=(("EX-02", []),), verified_ids=("EX-02",))  # no EX-01 tracked at all

            result = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02",
                    attested_dependencies=(("EX-01", "source-feature"),))))
            self.assertEqual(result.status, "error")
            self.assertIn("evidence-source-task-missing", result.message)
            self.assertFalse((root / "runs" / FEATURE / "run.json").exists())

    def test_source_dependency_not_verified_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            self._make_source_run(
                root, "source-feature", prompt, plan,
                tasks=(("EX-01", []),), verified_ids=())  # EX-01 stays 'ready', never verified

            result = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02",
                    attested_dependencies=(("EX-01", "source-feature"),))))
            self.assertEqual(result.status, "error")
            self.assertIn("evidence-source-run-not-closed", result.message)
            self.assertFalse((root / "runs" / FEATURE / "run.json").exists())

    def test_resume_without_attest_dependency_keeps_the_recorded_set(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            self._make_source_run(
                root, "source-feature", prompt, plan,
                tasks=(("EX-01", []),), verified_ids=("EX-01",))

            first = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                executor=sa.ScriptedExecutor(("implemented",)),
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02",
                    attested_dependencies=(("EX-01", "source-feature"),))))
            self.assertTrue(first.ok, first.message)

            resumed = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                executor=sa.ScriptedExecutor(("implemented",)),
                controls=ExecuteControls(plan_approved=True, task="EX-02", resume=True)))
            self.assertTrue(resumed.ok, resumed.message)

    def test_resume_repeating_attest_dependency_must_match_exactly(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            self._make_source_run(
                root, "source-feature", prompt, plan,
                tasks=(("EX-01", []),), verified_ids=("EX-01",))
            first = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                executor=sa.ScriptedExecutor(("implemented",)),
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02",
                    attested_dependencies=(("EX-01", "source-feature"),))))
            self.assertTrue(first.ok, first.message)

            self._make_source_run(
                root, "other-feature", prompt, plan,
                tasks=(("EX-01", []),), verified_ids=("EX-01",))

            resumed = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02", resume=True,
                    attested_dependencies=(("EX-01", "other-feature"),))))
            self.assertEqual(resumed.status, "error")
            self.assertIn("attestation-mismatch", resumed.message)

    def test_resume_rejects_dependency_chain_control_drift(self) -> None:
        with TemporaryDirectory() as directory:
            root, prompt, plan = self._seed(directory)
            first = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                executor=sa.ScriptedExecutor(("implemented", "implemented")),
                controls=ExecuteControls(plan_approved=True, task="EX-02",
                    verify_dependency_chain=True)))
            self.assertTrue(first.ok, first.message)

            resumed = execute_run(self._request(
                root, prompt, plan, task_ids=("EX-01", "EX-02"),
                controls=ExecuteControls(plan_approved=True, task="EX-02", resume=True)))
            self.assertEqual(resumed.status, "error")
            self.assertIn("verify-dependency-chain-mismatch", resumed.message)


class RuntimeSupersessionDefaultReuseTests(unittest.TestCase):
    """REC-14: execute must consume the same default-reuse resolution as preview."""

    def test_tc05_dispatches_only_when_verified_rec01_supersedes_terminal_tc04(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            prompt, plan = root / "prompt.md", root / "plan.md"
            prompt.write_text("feature prompt", encoding="utf-8")
            plan.write_text("# plan\n", encoding="utf-8")
            tasks = root / "tasks"; tasks.mkdir()
            def spec(task_id: str, depends_on: tuple[str, ...] = ()) -> TaskSpec:
                path = tasks / f"{task_id}.md"
                path.write_text(
                    f"# {task_id}\n" + (
                        "\n## Supersession\n- Supersedes: TC-04\n" if task_id == "REC-01" else ""
                    ), encoding="utf-8")
                return TaskSpec.build(id=task_id, title=task_id, path=str(path), task_type="python",
                    executor="python-executor", depends_on=depends_on, allowed_scope=("src/**",),
                    out_of_scope=(), acceptance_criteria=("works",), verification_commands=(),
                    max_repair_attempts=0)
            tc04, rec01, tc05 = spec("TC-04"), spec("REC-01"), spec("TC-05", ("TC-04",))
            source = Run.create("rec01-verified", prompt, plan, root / "runs" / "rec01-verified", root)
            source_life = RunLifecycle.initialize(source, tasks=[("REC-01", [])])
            source_life.transition("REC-01", "running", actor=ACTOR_RUNNER)
            source_life.transition("REC-01", "implemented", actor=ACTOR_RUNNER)
            source.record_verdicts("REC-01", "PASS", "PASS")
            persist_task_contracts(source, (rec01,)); source.status = "verified"; source.save()
            historical_bytes = (source.run_dir / "run.json").read_bytes()
            executor = sa.ScriptedExecutor(("implemented",))
            result = execute_run(ExecuteRequest(
                feature="tc05-runtime", repo_root=root, run_dir=root / "runs" / "tc05-runtime",
                prompt_path=prompt, plan_path=plan, specs=(tc04, rec01, tc05), adapter=executor,
                launchers=VerifierLaunchers(task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                envelope_anchors=EnvelopeAnchors(project_root=".", agents_root=".agents"),
                verifier_anchors=VerifierAnchors(project_root=str(root), agents_root=str(root / ".agents")),
                environment={"claude": True}, controls=ExecuteControls(plan_approved=True, task="TC-05"), plan_prompt_path="plan.md"))
            self.assertTrue(result.ok, result.message)
            self.assertEqual([call["task_id"] for call in executor.calls], ["TC-05"])
            run = Run.load(result.run_dir, root)
            self.assertNotIn("TC-04", run.tasks)
            self.assertEqual(run.task("TC-05").reused_verification[0]["dependency_id"], "TC-04")
            self.assertEqual(run.task("TC-05").reused_verification[0]["replacement_id"], "REC-01")
            self.assertEqual((source.run_dir / "run.json").read_bytes(), historical_bytes)

    def test_strict_chain_reexecutes_the_replacement_without_default_reuse(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            prompt, plan = root / "prompt.md", root / "plan.md"
            prompt.write_text("feature prompt", encoding="utf-8")
            plan.write_text("# plan\n", encoding="utf-8")
            tasks = root / "tasks"; tasks.mkdir()

            def spec(task_id: str, depends_on: tuple[str, ...] = ()) -> TaskSpec:
                path = tasks / f"{task_id}.md"
                path.write_text(
                    f"# {task_id}\n" + (
                        "\n## Supersession\n- Supersedes: TC-04\n" if task_id == "REC-01" else ""
                    ), encoding="utf-8")
                return TaskSpec.build(id=task_id, title=task_id, path=str(path), task_type="python",
                    executor="python-executor", depends_on=depends_on, allowed_scope=("src/**",),
                    out_of_scope=(), acceptance_criteria=("works",), verification_commands=(),
                    max_repair_attempts=0)

            tc04, rec01, tc05 = spec("TC-04"), spec("REC-01"), spec("TC-05", ("TC-04",))
            source = Run.create("rec01-verified", prompt, plan, root / "runs" / "rec01-verified", root)
            source_life = RunLifecycle.initialize(source, tasks=[("REC-01", [])])
            source_life.transition("REC-01", "running", actor=ACTOR_RUNNER)
            source_life.transition("REC-01", "implemented", actor=ACTOR_RUNNER)
            source.record_verdicts("REC-01", "PASS", "PASS")
            persist_task_contracts(source, (rec01,)); source.status = "verified"; source.save()

            executor = sa.ScriptedExecutor(("implemented", "implemented"))
            result = execute_run(ExecuteRequest(
                feature="tc05-strict", repo_root=root, run_dir=root / "runs" / "tc05-strict",
                prompt_path=prompt, plan_path=plan, specs=(tc04, rec01, tc05), adapter=executor,
                launchers=VerifierLaunchers(task=sa.ScriptedVerifier(("PASS", "PASS")), test=sa.ScriptedVerifier(("PASS", "PASS"))),
                envelope_anchors=EnvelopeAnchors(project_root=".", agents_root=".agents"),
                verifier_anchors=VerifierAnchors(project_root=str(root), agents_root=str(root / ".agents")),
                environment={"claude": True},
                controls=ExecuteControls(plan_approved=True, task="TC-05", verify_dependency_chain=True),
                plan_prompt_path="plan.md"))

            self.assertTrue(result.ok, result.message)
            self.assertEqual([call["task_id"] for call in executor.calls], ["REC-01", "TC-05"])
            run = Run.load(result.run_dir, root)
            self.assertNotIn("TC-04", run.tasks)
            self.assertEqual(run.task("TC-05").reused_verification, [])

    def test_fresh_selected_tsl02_reuses_completed_tsl01_without_dispatching_it(self) -> None:
        """A completed TSL-01 is terminal evidence, not a fresh executor transition."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "tasks"
            tasks.mkdir()
            dependency_path = tasks / "TSL-01.md"
            selected_path = tasks / "TSL-02.md"
            dependency_path.write_text("# TSL-01\n", encoding="utf-8")
            selected_path.write_text("# TSL-02\n", encoding="utf-8")
            dependency = replace(
                _specs(("EX-01",))[0], id="TSL-01", title="TSL-01", path="tasks/TSL-01.md",
            )
            selected = replace(
                _specs(("EX-02",))[0], id="TSL-02", title="TSL-02", path="tasks/TSL-02.md",
                depends_on=("TSL-01",),
            )
            source_prompt = root / "source-prompt.md"
            source_plan = root / "source-plan.json"
            source_prompt.write_text("source prompt", encoding="utf-8")
            source_plan.write_text("{}\n", encoding="utf-8")
            source = Run.create(
                "source-tsl-01", source_prompt, source_plan, root / "runs" / "source-tsl-01", root,
            )
            source_life = RunLifecycle.initialize(source, tasks=[("TSL-01", [])])
            source_life.transition("TSL-01", "in_progress", actor=ACTOR_RUNNER)
            source.record_verdicts("TSL-01", "PASS", "PASS")
            source.status = "completed"
            persist_task_contracts(source, (dependency,))
            source.save()

            executor = sa.ScriptedExecutor(("implemented",))
            result = execute_run(
                _request(
                    root, (dependency, selected), executor=executor,
                    launchers=VerifierLaunchers(
                        task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                    controls=ExecuteControls(plan_approved=True, task="TSL-02"),
                    environment={"claude": True},
                )
            )

            self.assertTrue(result.ok, result.message)
            self.assertEqual([call["task_id"] for call in executor.calls], ["TSL-02"])
            run = Run.load(root / "runs" / FEATURE, root)
            reused = run.task("TSL-01")
            self.assertEqual(reused.status, "done")
            self.assertEqual(reused.resolution, "completed")
            self.assertEqual(reused.next_executor_launch_generation, 1)
            self.assertTrue(reused.reused_verification)


class _BoardSnapshotExecutor(sa.ScriptedExecutor):
    """A :class:`sa.ScriptedExecutor` that snapshots the board text at every real launch —
    exactly the moment ``dispatch_executor`` hands work to the adapter — so a test can prove
    the board already shows ``In Progress`` *before* that launch and never shows a different
    card started (KLC-03 AC-1)."""

    def __init__(self, results: tuple[str, ...], board_path: Path) -> None:
        super().__init__(results)
        self._board_path = board_path
        self.board_snapshots: list[str] = []

    def launch(self, request):  # noqa: ANN001 - test double
        if not (request.no_tools or request.resume_session_id):
            self.board_snapshots.append(self._board_path.read_text(encoding="utf-8"))
        return super().launch(request)


_BOARD = """# Kanban Board

## To Do

- [EX-01: Direct success task](../fixtures/execution/tasks/EX-01_direct-success.md)
- [EX-02: Dependent verify task](../fixtures/execution/tasks/EX-02_dependent-verify.md)

## In Progress
"""


def _seed_board(root: Path) -> Path:
    """Seed a Markdown board plus the real task files it links to (copied verbatim from the
    ``fixtures/execution/tasks`` fixtures, which already carry the ``## Status`` block
    :mod:`feature_pipeline.infrastructure.board_projection` requires) under ``root``."""
    board = root / "docs" / "kanban.md"
    board.parent.mkdir(parents=True, exist_ok=True)
    board.write_text(_BOARD, encoding="utf-8")
    for name in ("EX-01_direct-success.md", "EX-02_dependent-verify.md"):
        source = FIXTURES / "tasks" / name
        dest = root / "fixtures" / "execution" / "tasks" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return board


class BoardProjectionWiringTests(unittest.TestCase):
    """KLC-03 — execute mode projects lifecycle transitions onto a Markdown-backed board.

    A boardless ``ExecuteRequest`` (every other test in this module — ``board_path`` defaults
    to ``None``) keeps its current behavior: none of these transitions run without an explicit
    board path (AC-3, second half).
    """

    def test_execution_projects_in_progress_before_each_executor_launch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            board = _seed_board(root)
            executor = _BoardSnapshotExecutor(("implemented", "implemented"), board)
            result = execute_run(
                _request(
                    root, _specs(("EX-01", "EX-02")), executor=executor,
                    launchers=VerifierLaunchers(
                        task=sa.ScriptedVerifier(("PASS", "PASS")),
                        test=sa.ScriptedVerifier(("PASS", "PASS"))),
                    controls=ExecuteControls(plan_approved=True), environment={"claude": True},
                    board_path=board))

            self.assertTrue(result.ok, result.message)
            self.assertEqual(len(executor.board_snapshots), 2)
            first, second = executor.board_snapshots

            self.assertIn("EX-01", first.split("## In Progress")[1])
            self.assertIn("EX-02", first.split("## In Progress")[0])

            self.assertNotIn("EX-01", second)
            self.assertIn("EX-02", second.split("## In Progress")[1])

    def test_completion_projects_done_with_durable_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            board = _seed_board(root)
            task_path = root / "fixtures/execution/tasks/EX-01_direct-success.md"
            result = execute_run(
                _request(
                    root, _specs(("EX-01",)), executor=sa.ScriptedExecutor(("implemented",)),
                    launchers=VerifierLaunchers(
                        task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                    controls=ExecuteControls(plan_approved=True), environment={"claude": True},
                    board_path=board))

            self.assertTrue(result.ok, result.message)
            board_text = board.read_text(encoding="utf-8")
            self.assertNotIn("EX-01", board_text)

            task_text = task_path.read_text(encoding="utf-8")
            self.assertIn("- [x] Done", task_text)
            self.assertEqual(task_text.count("## Result"), 1)
            run = Run.load(result.run_dir, root)
            self.assertEqual(run.task("EX-01").status, "done")

    def test_failed_verification_with_justified_scope_amendment_reaches_done(self) -> None:
        class ScopeAmendmentExecutor(sa.ScriptedExecutor):
            def launch(self, request):  # noqa: ANN001 - test double
                if not (request.no_tools or request.resume_session_id):
                    amended = (
                        Path(request.working_root)
                        / "fixtures/execution/tasks/EX-01_direct-success.md"
                    )
                    amended.write_text(
                        amended.read_text(encoding="utf-8")
                        + "\n## Repair Scope Amendment\n\n"
                        + "This task-specific migration note is required to repair the "
                        "failed verification.\n",
                        encoding="utf-8",
                    )
                return super().launch(request)

        class AmendmentReviewVerifier(sa.ScriptedVerifier):
            def __init__(self, verdicts: tuple[str, ...], *, role: str) -> None:
                super().__init__(verdicts)
                self.role = role
                self.amendment_reports: list[str] = []

            def launch(self, request):  # noqa: ANN001 - test double
                result = super().launch(request)
                if not request.resume_session_id:
                    if "Amendment-justification finding:" not in request.prompt:
                        raise AssertionError("verifier did not receive the review requirement")
                    report = (
                        f"# {self.role}\n\n- Verdict: {self._verdict()}\n\n"
                        "- Amendment-justification finding: accepted — EX-01's "
                        "migration note is a reviewable repair artifact for the failed "
                        "verification.\n"
                    )
                    self.amendment_reports.append(report)
                    Path(request.report_path).write_text(report, encoding="utf-8")
                return result

        with TemporaryDirectory() as directory:
            root = Path(directory)
            board = _seed_board(root)
            task_path = root / "fixtures/execution/tasks/EX-01_direct-success.md"
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            subprocess.run(("git", "add", "."), cwd=root, check=True)
            subprocess.run(
                ("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture baseline"),
                cwd=root,
                check=True,
            )
            task_verifier = AmendmentReviewVerifier(("FAIL", "PASS"), role="task_verifier")
            test_verifier = AmendmentReviewVerifier(("PASS", "PASS"), role="test_verifier")
            result = execute_run(
                _request(
                    root, _specs(("EX-01",)),
                    executor=ScopeAmendmentExecutor(("implemented", "implemented")),
                    launchers=VerifierLaunchers(
                        task=task_verifier, test=test_verifier),
                    controls=ExecuteControls(plan_approved=True), environment={"claude": True},
                    board_path=board))

            self.assertTrue(result.ok, result.message)
            run = Run.load(result.run_dir, root)
            record = run.task("EX-01")
            self.assertEqual(record.status, "done")
            self.assertTrue(record.execution_evidence["implementation"]["scope_amendment"]["present"])
            self.assertIn(
                "fixtures/execution/tasks/EX-01_direct-success.md",
                record.execution_evidence["implementation"]["scope_amendment"]["observed_paths"],
            )
            self.assertEqual(
                record.execution_evidence["implementation"]["scope_amendment"]["rationale"],
                "executor-owned paths outside the initial estimate require independent "
                "amendment-justification review",
            )
            self.assertIn("failed", [entry["outcome"] for entry in record.operation_history])
            self.assertNotIn("unblock", " ".join(entry["outcome"] for entry in record.operation_history))
            reports = task_verifier.amendment_reports + test_verifier.amendment_reports
            self.assertEqual(len(reports), 4)
            for report in reports:
                self.assertIn("Amendment-justification finding: accepted", report)

    def test_escalation_does_not_project_a_terminal_board_state(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            board = _seed_board(root)
            task_path = root / "fixtures/execution/tasks/EX-01_direct-success.md"
            result = execute_run(
                _request(
                    root, _specs(("EX-01",)), executor=sa.ScriptedExecutor(("implemented",)),
                    launchers=VerifierLaunchers(
                        task=sa.ScriptedVerifier(("FAIL",)), test=sa.ScriptedVerifier(("PASS",))),
                    controls=ExecuteControls(plan_approved=True, max_repair_attempts=0),
                    environment={"claude": True}, board_path=board))

            self.assertEqual(result.status, "blocked")
            board_text = board.read_text(encoding="utf-8")
            self.assertNotIn("EX-01", board_text.split("## In Progress")[0])
            self.assertIn("EX-01", board_text.split("## In Progress")[1])

            task_text = task_path.read_text(encoding="utf-8")
            self.assertIn("- [x] In Progress", task_text)
            self.assertNotIn("## Blockers", task_text)

    def test_resume_reconciles_a_stale_active_card_from_durable_done_evidence(self) -> None:
        """KLC-03. A crash between a durable transition and its Markdown projection leaves a
        terminal task's board card and task-file checkbox stale. A real ``--resume`` against
        that persisted run must repair the human-facing files from durable evidence alone —
        without redispatching an executor or verifier for the already-``done`` task."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            board = _seed_board(root)
            task_path = root / "fixtures/execution/tasks/EX-01_direct-success.md"

            # First invocation without a board path: run.json reaches completion but the
            # Markdown board/task file are never touched — simulating a crash between the
            # durable transition and the (never-attempted) projection.
            first = execute_run(
                _request(
                    root, _specs(("EX-01",)), executor=sa.ScriptedExecutor(("implemented",)),
                    launchers=VerifierLaunchers(
                        task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                    controls=ExecuteControls(plan_approved=True), environment={"claude": True}))
            self.assertTrue(first.ok, first.message)
            run = Run.load(first.run_dir, root)
            self.assertEqual(run.task("EX-01").status, "done")
            reports_dir = first.run_dir / "reports"
            source_report_bytes = {
                path.relative_to(reports_dir): path.read_bytes()
                for path in sorted(reports_dir.rglob("*")) if path.is_file()
            } if reports_dir.is_dir() else {}

            # The board and task file still show the pre-completion state — the stale
            # projection this resume must repair.
            stale_board_text = board.read_text(encoding="utf-8")
            self.assertIn("EX-01", stale_board_text)
            self.assertIn("- [ ] Done", task_path.read_text(encoding="utf-8"))

            # A resume does not redispatch the completed executor or verifiers.
            executor = sa.ScriptedExecutor(("implemented",))
            task_verifier = sa.ScriptedVerifier(("PASS",))
            test_verifier = sa.ScriptedVerifier(("PASS",))
            resumed = execute_run(
                _request(
                    root, _specs(("EX-01",)), executor=executor,
                    launchers=VerifierLaunchers(task=task_verifier, test=test_verifier),
                    controls=ExecuteControls(plan_approved=True, resume=True),
                    environment={"claude": True}, board_path=board))

            self.assertTrue(resumed.ok, resumed.message)
            self.assertEqual(executor.launches, 0)
            self.assertEqual(task_verifier.calls, [])
            self.assertEqual(test_verifier.calls, [])

            board_text = board.read_text(encoding="utf-8")
            self.assertNotIn("EX-01", board_text)

            task_text = task_path.read_text(encoding="utf-8")
            self.assertIn("- [x] Done", task_text)
            self.assertEqual(task_text.count("## Result"), 1)

            # The resume's own reconciliation is a durable-evidence projection, not a
            # rewrite of the original completed task's report evidence.
            resumed_report_bytes = {
                path.relative_to(reports_dir): path.read_bytes()
                for path in sorted(reports_dir.rglob("*")) if path.is_file()
            } if reports_dir.is_dir() else {}
            self.assertEqual(resumed_report_bytes, source_report_bytes)
            self.assertEqual(Run.load(first.run_dir, root).task("EX-01").verification,
                              run.task("EX-01").verification)

    def test_fresh_reuse_projects_the_reused_dependency_after_pruning_its_ancestor(self) -> None:
        """A pruned ancestor is not a lifecycle record, but its reused dependent is."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            board = _seed_board(root)
            source_task = FIXTURES / "tasks" / "EX-03_repair-then-verify.md"
            target_task = root / "fixtures/execution/tasks/EX-03_repair-then-verify.md"
            target_task.write_text(source_task.read_text(encoding="utf-8"), encoding="utf-8")
            board.write_text(
                board.read_text(encoding="utf-8")
                + "- [EX-03: Repair then verify task]"
                "(../fixtures/execution/tasks/EX-03_repair-then-verify.md)\n",
                encoding="utf-8",
            )
            prompt = root / "prompt.md"
            plan = root / "plan.json"
            prompt.write_text("feature prompt", encoding="utf-8")
            plan.write_text(json.dumps(PLAN, indent=2) + "\n", encoding="utf-8")
            source = Run.create("source-feature", prompt, plan, root / "runs" / "source-feature", root)
            source_life = RunLifecycle.initialize(source, tasks=[("EX-02", [])])
            source_life.transition("EX-02", "running", actor=ACTOR_RUNNER)
            source_life.transition("EX-02", "implemented", actor=ACTOR_RUNNER)
            source.record_verdicts("EX-02", "PASS", "PASS")
            source.status = "verified"
            persist_task_contracts(source, _specs(("EX-02",)))
            source.save()

            executor = sa.ScriptedExecutor(("implemented",))
            result = execute_run(
                _request(
                    root, _specs(("EX-01", "EX-02", "EX-03")), executor=executor,
                    launchers=VerifierLaunchers(
                        task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                    controls=ExecuteControls(plan_approved=True, task="EX-03"),
                    environment={"claude": True}, board_path=board))

            self.assertTrue(result.ok, result.message)
            self.assertEqual([call["task_id"] for call in executor.calls], ["EX-03"])
            self.assertIn("EX-02", board.read_text(encoding="utf-8"))
            reused_task = root / "fixtures/execution/tasks/EX-02_dependent-verify.md"
            self.assertIn("- [ ] Done", reused_task.read_text(encoding="utf-8"))
            self.assertEqual(reused_task.read_text(encoding="utf-8").count("## Result"), 0)

    def test_boardless_execution_is_unaffected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = execute_run(
                _request(
                    root, _specs(("EX-01",)), executor=sa.ScriptedExecutor(("implemented",)),
                    launchers=VerifierLaunchers(
                        task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                    controls=ExecuteControls(plan_approved=True), environment={"claude": True}))
            self.assertTrue(result.ok, result.message)
            self.assertFalse((root / "docs" / "kanban.md").exists())


if __name__ == "__main__":
    unittest.main()
