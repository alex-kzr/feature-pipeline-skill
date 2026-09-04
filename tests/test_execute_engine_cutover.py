"""PE-02 — stages 5-9 run through the one ``PipelineEngine`` in production ``execute`` mode.

``docs/plans/tasks/PE-02_pipeline-engine-cutover.md``. PE-01 built the stage contracts and the
engine; PE-02 makes the *only* production stage-5-9 path — the ``execute`` task loop in
:mod:`pipeline_core.execution` — drive every dependency-ready task through it, whenever a
:class:`~feature_pipeline.domain.plan.CompiledRunPlan` is present (every real CLI ``execute``
invocation, CP-02). These tests pin the cutover itself, reusing the same deterministic fake
adapters and fixture plan :mod:`tests.test_execute_mode` already exercises without a compiled
plan:

* AC-1 — a compiled-plan ``execute`` run reaches the same terminal shape (status, exit code,
  persisted run state, ``TaskRunResult`` fields) as the pre-existing plan-less scenario;
* AC-2 — with a compiled plan present, :func:`pipeline_core.execution.run_task` (the pre-CP-02
  direct call) is never reached — the engine is the only path; the reverse holds with no
  compiled plan (the documented, unchanged legacy surface);
* stages 5/6 (:func:`pipeline_core.execution._task_selection_stage` /
  ``_executor_routing_stage``) record the already-resolved selection/route as evidence and
  never re-decide it.

Standard library only.
"""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from feature_pipeline.application.compile_plan import (
    ControlOverrides,
    ShallowTaskInput,
    compile_run_plan,
)
from feature_pipeline.application.profile_bridge import compiled_profile_from_core
from feature_pipeline.contracts import Profile
from feature_pipeline.domain.plan import CompiledRunPlan
from feature_pipeline.domain.stages import StageContext, StageStatus
from feature_pipeline.domain.vocabulary import StageId
from pipeline_core.execution import (
    EXIT_OK,
    ExecuteControls,
    ExecuteRequest,
    _executor_routing_stage,
    _run_selected_task,
    _task_selection_stage,
    execute_run,
)
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.state import Run
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


def _profile() -> dict:
    return {
        "version": 1,
        "name": "engine-cutover-project",
        "logical_paths": {"project": ".", "agents": ".agents", "core": "."},
        "role_grants": {
            "executor": ["read", "write"],
            "task_verifier": ["read"],
            "test_verifier": ["read", "run_checks"],
        },
        "stages": [
            {"name": "implement", "subagents": ["executor"], "argv": ["do", "apply"]},
            {"name": "verification",
             "subagents": ["task_verifier", "test_verifier"], "argv": ["do", "check"]},
        ],
        "registry": {
            "task_types": {
                "python": {
                    "stack": "py", "subagents": ["executor"],
                    "root": "code", "checks": ["lint"], "storage": "runs",
                },
            },
            "stacks": {"py": {"runtime": "py"}},
            "subagents": {
                "executor": {"grant": "executor"},
                "task_verifier": {"grant": "task_verifier"},
                "test_verifier": {"grant": "test_verifier"},
            },
            "roots": {"code": "code"},
            "checks": {"lint": ["lint", "run"]},
            "storage": {"runs": ".pipeline/runs"},
        },
    }


def _compiled_plan(specs: tuple[TaskSpec, ...]) -> CompiledRunPlan:
    profile = compiled_profile_from_core(Profile.from_data(_profile()))
    definitions = [
        ShallowTaskInput(
            s.id, s.task_type, tuple(s.depends_on), s.executor,
            tuple(s.allowed_scope), tuple(s.out_of_scope), s.max_repair_attempts,
        )
        for s in specs
    ]
    return compile_run_plan(
        feature=FEATURE, definitions=definitions, profile=profile,
        overrides=ControlOverrides(adapter="claude"),
    )


def _request(
    root: Path,
    specs: tuple[TaskSpec, ...],
    *,
    executor,
    launchers: VerifierLaunchers,
    controls: ExecuteControls,
    environment: dict,
    compiled_plan: CompiledRunPlan | None,
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
        compiled_plan=compiled_plan,
    )


def _controls(scenario: sa.Scenario) -> ExecuteControls:
    overrides = dict(scenario.controls)
    gated = not overrides.pop("_no_gate", False)
    return ExecuteControls(plan_approved=gated, **overrides)


def _run_scenario(name: str, root: Path, *, with_plan: bool):
    scenario = sa.SCENARIOS[name]
    specs = _specs(scenario.task_ids)
    executor = scenario.executor()
    result = execute_run(
        _request(
            root, specs, executor=executor,
            launchers=VerifierLaunchers(
                task=scenario.task_verifier(), test=scenario.test_verifier()),
            controls=_controls(scenario), environment={"claude": True},
            compiled_plan=_compiled_plan(specs) if with_plan else None))
    return result, root


class ComparableTerminalShapeTests(unittest.TestCase):
    """AC-1: a compiled-plan run reaches the same terminal shape as the plan-less path."""

    def test_direct_success_is_identical_with_and_without_a_compiled_plan(self) -> None:
        with TemporaryDirectory() as legacy_dir, TemporaryDirectory() as engine_dir:
            legacy, _ = _run_scenario("direct-success", Path(legacy_dir), with_plan=False)
            engine, engine_root = _run_scenario(
                "direct-success", Path(engine_dir), with_plan=True)

            self.assertEqual(
                (legacy.status, legacy.exit_code), (engine.status, engine.exit_code))
            self.assertEqual(engine.exit_code, EXIT_OK)
            self.assertEqual(len(engine.task_results), len(legacy.task_results))
            legacy_outcome, engine_outcome = legacy.task_results[0], engine.task_results[0]
            self.assertEqual(
                (legacy_outcome.task_id, legacy_outcome.status, legacy_outcome.attempts,
                 legacy_outcome.gates),
                (engine_outcome.task_id, engine_outcome.status, engine_outcome.attempts,
                 engine_outcome.gates),
            )
            self.assertIsNone(engine_outcome.diagnostic)

            run = Run.load(engine.run_dir, engine_root)
            self.assertEqual(run.task("EX-01").status, "verified")
            self.assertEqual(run.task("EX-01").verification["task_verdict"], "PASS")

    def test_successful_repair_is_identical_with_and_without_a_compiled_plan(self) -> None:
        with TemporaryDirectory() as legacy_dir, TemporaryDirectory() as engine_dir:
            legacy, _ = _run_scenario("successful-repair", Path(legacy_dir), with_plan=False)
            engine, _ = _run_scenario("successful-repair", Path(engine_dir), with_plan=True)

            self.assertEqual(
                (legacy.status, legacy.exit_code), (engine.status, engine.exit_code))
            legacy_outcome = legacy.task_results[-1]
            engine_outcome = engine.task_results[-1]
            self.assertEqual(legacy_outcome.status, "verified")
            self.assertEqual(
                (legacy_outcome.attempts, legacy_outcome.gates),
                (engine_outcome.attempts, engine_outcome.gates),
            )
            self.assertGreaterEqual(engine_outcome.attempts, 1)  # a repair actually happened

    def test_repair_exhaustion_is_identical_with_and_without_a_compiled_plan(self) -> None:
        with TemporaryDirectory() as legacy_dir, TemporaryDirectory() as engine_dir:
            legacy, _ = _run_scenario("repair-exhaustion", Path(legacy_dir), with_plan=False)
            engine, _ = _run_scenario("repair-exhaustion", Path(engine_dir), with_plan=True)

            self.assertEqual(
                (legacy.status, legacy.exit_code), (engine.status, engine.exit_code))
            self.assertIsNotNone(engine.task_results[-1].diagnostic)
            self.assertTrue(engine.task_results[-1].diagnostic.is_file())


class OnlyOneProductionPathTests(unittest.TestCase):
    """AC-2: no second production stage sequence remains."""

    def test_compiled_plan_present_never_calls_the_pre_cp02_run_task(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "pipeline_core.execution.run_task",
            side_effect=AssertionError("run_task must not be called when a plan is compiled"),
        ):
            result, _ = _run_scenario("direct-success", Path(directory), with_plan=True)
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_no_compiled_plan_never_touches_the_pipeline_engine(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "pipeline_core.execution.PipelineEngine",
            side_effect=AssertionError("PipelineEngine must not run without a compiled plan"),
        ):
            result, _ = _run_scenario("direct-success", Path(directory), with_plan=False)
        self.assertEqual(result.exit_code, EXIT_OK)


class SelectionAndRoutingStagesTests(unittest.TestCase):
    """Stages 5/6 record the already-resolved decision; they never re-decide it."""

    def _plan(self) -> CompiledRunPlan:
        return _compiled_plan(_specs(("EX-01",)))

    def test_task_selection_stage_records_the_chosen_task_id(self) -> None:
        context = StageContext(
            plan=self._plan(), stage=StageId.TASK_SELECTION,
            resources={"selected_task_id": "EX-01"},
        )
        outcome = _task_selection_stage(context)
        self.assertEqual(outcome.stage, StageId.TASK_SELECTION)
        self.assertEqual(outcome.status, StageStatus.COMPLETED)
        self.assertEqual(outcome.evidence["task_id"], "EX-01")

    def test_executor_routing_stage_reads_the_route_off_the_compiled_plan(self) -> None:
        plan = self._plan()
        context = StageContext(
            plan=plan, stage=StageId.EXECUTOR_ROUTING,
            resources={"selected_task_id": "EX-01"},
        )
        outcome = _executor_routing_stage(context)
        resolved = plan.task("EX-01")
        self.assertEqual(outcome.stage, StageId.EXECUTOR_ROUTING)
        self.assertEqual(outcome.status, StageStatus.COMPLETED)
        self.assertEqual(outcome.evidence["adapter"], resolved.adapter)
        self.assertEqual(outcome.evidence["role"], resolved.role)


class RunSelectedTaskUnitTests(unittest.TestCase):
    """``_run_selected_task`` is the one seam ``execute_run`` calls per task."""

    def test_none_plan_delegates_to_run_task(self) -> None:
        sentinel = object()
        with patch("pipeline_core.execution.run_task", return_value=sentinel) as fake:
            result = _run_selected_task(life=object(), plan=None, task_id="EX-01", request=object())
        fake.assert_called_once_with(fake.call_args.args[0], fake.call_args.args[1])
        self.assertIs(result, sentinel)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
