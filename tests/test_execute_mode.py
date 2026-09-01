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
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.state import ACTOR_RUNNER, Run, pid_alive
from pipeline_core.verification import VerifierAnchors, VerifierLaunchers
from schemas.contracts import TaskSpec

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
            # EX-01 has no dependency of its own so it is naturally 'ready'; the point is it
            # was never dispatched to reach that (or any later) state.
            self.assertEqual(run.task("EX-01").status, "ready")
            attested = run.task("EX-02").attested_dependencies
            self.assertEqual(len(attested), 1)
            self.assertEqual(attested[0]["dep_id"], "EX-01")
            self.assertEqual(attested[0]["source_feature"], "source-feature")
            self.assertEqual(attested[0]["task_verdict"], "PASS")
            self.assertTrue(attested[0]["source_digest"].startswith("sha256:"))

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
            self.assertIn("attestation-source-missing", result.message)
            self.assertFalse((root / "runs" / FEATURE / "run.json").exists())

    def test_prompt_plan_identity_mismatch_is_refused(self) -> None:
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
            self.assertEqual(result.status, "error")
            self.assertIn("attestation-source-identity-mismatch", result.message)
            self.assertFalse((root / "runs" / FEATURE / "run.json").exists())

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
            self.assertIn("attestation-source-dependency-absent", result.message)
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
            self.assertIn("attestation-source-not-verified", result.message)
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
            self._make_source_run(
                root, "other-feature", prompt, plan,
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
                controls=ExecuteControls(
                    plan_approved=True, task="EX-02", resume=True,
                    attested_dependencies=(("EX-01", "other-feature"),))))
            self.assertEqual(resumed.status, "error")
            self.assertIn("attestation-mismatch", resumed.message)


if __name__ == "__main__":
    unittest.main()
