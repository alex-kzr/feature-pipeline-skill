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
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline_core.concurrency import pipeline_lock_path
from pipeline_core.execution import (
    EXIT_BLOCKED,
    EXIT_ERROR,
    EXIT_OK,
    ExecuteControls,
    ExecuteRequest,
    execute_run,
)
from pipeline_core.adapters import LaunchResult
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.state import ACTOR_RUNNER, Run, pid_alive
from pipeline_core.verification import VerifierAnchors, VerifierLaunchers
from feature_pipeline.contracts import TaskSpec

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


class ScenarioTableTests(unittest.TestCase):
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
            self.assertEqual(run.task("EX-01").status, "verified")
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
            self.assertIsNotNone(result.task_results[0].diagnostic)
            self.assertTrue(result.task_results[0].diagnostic.is_file())
            self.assertEqual(Run.load(result.run_dir, root).task("EX-01").status, "blocked")

    def test_dependency_suppression_never_dispatches_the_suppressed_tasks(self) -> None:
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
            self.assertEqual(run.task("EX-01").status, "blocked")
            self.assertTrue((run.task("EX-02").blocker or "").startswith("blocked_by: "))
            self.assertTrue((run.task("EX-03").blocker or "").startswith("blocked_by: "))

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
            reloaded.task("EX-01").status = "verification_failed"
            reloaded.save()

            executor = sa.ScriptedExecutor(("implemented",))
            resumed = self._run_once(
                root, executor=executor,
                task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",)),
                controls=ExecuteControls(plan_approved=True, resume=True))

            self.assertTrue(resumed.ok)
            self.assertEqual(resumed.exit_code, EXIT_OK)
            run = Run.load(resumed.run_dir, root)
            self.assertEqual(run.task("EX-01").status, "verified")
            self.assertEqual(run.task("EX-01").attempts, 1)  # not re-counted on resume
            self.assertEqual(run.controls["adapter_resolved"]["value"], "claude")
            self.assertTrue(executor.calls[0]["is_repair"])

    def test_resume_retries_codex_protocol_failure_with_a_new_generation(self) -> None:
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
            self.assertEqual(run.task("EX-01").status, "running")
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

            self.assertTrue(resumed.ok)
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
            self.assertEqual(run.task("EX-02").status, "verified")
            self.assertEqual(run.task("EX-01").status, "verified")
            reused = run.task("EX-01").reused_verification
            self.assertEqual(len(reused), 1)
            self.assertEqual(reused[0]["dependency_id"], "EX-01")
            self.assertEqual(reused[0]["task_verdict"], "PASS")
            self.assertTrue(reused[0]["source_run_digest"].startswith("sha256:"))

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
            self.assertEqual(run.task("EX-02").status, "verified")
            self.assertEqual(run.task("EX-03").status, "verified")

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

    def test_start_moves_only_the_selected_task_before_launch(self) -> None:
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

            # Before EX-01's launch: EX-01 in In Progress, EX-02 untouched in To Do.
            self.assertIn("EX-01", first.split("## In Progress")[1])
            self.assertIn("EX-02", first.split("## In Progress")[0])

            # Before EX-02's launch: EX-01 already verified (no card at all), EX-02 moved.
            self.assertNotIn("EX-01", second)
            self.assertIn("EX-02", second.split("## In Progress")[1])

    def test_verified_completion_removes_card_and_writes_a_result(self) -> None:
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
            self.assertIn(run.run_id, task_text)
            self.assertIn("outcome: **verified**", task_text)

    def test_blocked_task_returns_to_to_do_with_blockers_preserved(self) -> None:
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
            self.assertIn("EX-01", board_text.split("## In Progress")[0])
            self.assertNotIn("EX-01", board_text.split("## In Progress")[1])

            task_text = task_path.read_text(encoding="utf-8")
            self.assertIn("- [x] To Do", task_text)
            self.assertIn("## Blockers", task_text)

    def test_resume_reconciles_a_verified_task_without_redispatching_it(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            board = _seed_board(root)
            task_path = root / "fixtures/execution/tasks/EX-01_direct-success.md"

            # First invocation without a board path: run.json reaches 'verified' but the
            # Markdown board/task file are never touched — simulating a crash between the
            # durable transition and the (never-attempted) projection.
            first = execute_run(
                _request(
                    root, _specs(("EX-01",)), executor=sa.ScriptedExecutor(("implemented",)),
                    launchers=VerifierLaunchers(
                        task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                    controls=ExecuteControls(plan_approved=True), environment={"claude": True}))
            self.assertTrue(first.ok, first.message)
            self.assertIn("EX-01", board.read_text(encoding="utf-8"))

            # A resume that now names the board path reconciles the human view without
            # dispatching the already-verified executor again.
            executor = sa.ScriptedExecutor(("implemented",))
            resumed = execute_run(
                _request(
                    root, _specs(("EX-01",)), executor=executor,
                    launchers=VerifierLaunchers(
                        task=sa.ScriptedVerifier(("PASS",)), test=sa.ScriptedVerifier(("PASS",))),
                    controls=ExecuteControls(plan_approved=True, resume=True),
                    environment={"claude": True}, board_path=board))

            self.assertTrue(resumed.ok, resumed.message)
            self.assertEqual(executor.launches, 0)
            board_text = board.read_text(encoding="utf-8")
            self.assertNotIn("EX-01", board_text)
            self.assertIn("- [x] Done", task_path.read_text(encoding="utf-8"))

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
            self.assertNotIn("EX-02", board.read_text(encoding="utf-8"))
            reused_task = root / "fixtures/execution/tasks/EX-02_dependent-verify.md"
            self.assertIn("- [x] Done", reused_task.read_text(encoding="utf-8"))
            self.assertEqual(reused_task.read_text(encoding="utf-8").count("## Result"), 1)

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
