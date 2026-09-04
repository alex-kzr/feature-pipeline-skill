"""CP-02 - dry-run, execute, and resume all consume one ``CompiledRunPlan``.

Plan: ``docs/plans/tasks/CP-02_compiled-plan-production-cutover.md`` (AC-1, AC-2, AC-3).
CP-01 built the compiler and the immutable plan; CP-02 cuts the production paths over to it.
These tests pin the cutover itself:

* AC-1 - the dry-run preview and the ``execute`` path resolve one plan through the same
  compiler (identical selection, route, checks and resolved controls), and the dry-run text
  is rendered from that object;
* AC-2 - an unroutable task type or a selection that spans two run-storage roots is rejected
  before any run state, lease, artifact, or process exists;
* AC-3 - a resume whose freshly compiled plan differs from the recorded one fails closed,
  naming the first divergent field, without mutating ``run.json``.

Standard library only.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from feature_pipeline.application.compile_plan import (
    ControlOverrides,
    ShallowTaskInput,
    compile_run_plan,
)
from feature_pipeline.application.profile_bridge import compiled_profile_from_core
from feature_pipeline.contracts import Profile, TaskSpec
from pipeline_core import runner_cli
from pipeline_core.execution import ExecutionError, _ensure_plan_compatible, _plan_fingerprint
from pipeline_core.state import Run


# --- fixtures ----------------------------------------------------------------------------


def _native_profile(*, two_storage: bool = False) -> dict:
    """A native ``schemas.Profile`` document with a closed registry."""
    return {
        "version": 1,
        "name": "cutover-project",
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
                "docs": {
                    "stack": "text", "subagents": ["executor"],
                    "root": "content", "checks": ["lint"], "storage": "runs",
                },
                "python": {
                    "stack": "py", "subagents": ["executor"],
                    "root": "code", "checks": ["lint"],
                    "storage": "runs2" if two_storage else "runs",
                },
            },
            "stacks": {"text": {"runtime": "text"}, "py": {"runtime": "py"}},
            "subagents": {
                "executor": {"grant": "executor"},
                "task_verifier": {"grant": "task_verifier"},
                "test_verifier": {"grant": "test_verifier"},
            },
            "roots": {"content": "content", "code": "code"},
            "checks": {"lint": ["lint", "run"]},
            "storage": {"runs": ".pipeline/runs", "runs2": ".pipeline/other"},
        },
    }


def _seed(directory: str, *, plan_tasks: list[dict], profile: dict) -> dict:
    root = Path(directory)
    (root / ".agents" / "skills").mkdir(parents=True, exist_ok=True)
    (root / "profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    (root / "plan.json").write_text(
        json.dumps({"feature": "cutover-feature", "tasks": plan_tasks}, indent=2),
        encoding="utf-8",
    )
    (root / "prompt.md").write_text("feature prompt", encoding="utf-8")
    return {
        "anchors": [
            "--project-root", str(root),
            "--agents-root", str(root / ".agents"),
            "--core-root", str(root),
        ],
        "root": root,
    }


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = runner_cli.main(argv)
    return code, out.getvalue(), err.getvalue()


_FULL_JSON_TASK = {
    "task_type": "docs",
    "executor": "docs-executor",
    "allowed_scope": ["content/**"],
    "acceptance_criteria": ["It is done."],
    "verification_commands": [],
}


# --- AC-1 - one compiler, one plan object --------------------------------------------


class OneCompiledPlanTests(unittest.TestCase):
    def test_preview_and_execute_inputs_resolve_the_same_route_and_controls(self) -> None:
        """The dry-run preview feeds ``ShallowTaskInput`` from the plan table; ``_execute``
        feeds ``ShallowTaskInput`` derived from each ``TaskSpec``. Both go through the one
        ``compile_run_plan`` and resolve the same selection, route, checks and controls."""
        profile = compiled_profile_from_core(Profile.from_data(_native_profile()))

        preview_inputs = [
            ShallowTaskInput("DA-01", "docs"),
            ShallowTaskInput("DA-02", "docs", ("DA-01",)),
        ]
        specs = [
            TaskSpec.build(
                id="DA-01", task_type="docs", executor="docs-executor",
                allowed_scope=["content/**"], acceptance_criteria=["done"],
            ),
            TaskSpec.build(
                id="DA-02", task_type="docs", executor="docs-executor",
                allowed_scope=["content/**"], depends_on=["DA-01"],
                acceptance_criteria=["done"],
            ),
        ]
        execute_inputs = [
            ShallowTaskInput(
                s.id, s.task_type, tuple(s.depends_on), s.executor,
                tuple(s.allowed_scope), tuple(s.out_of_scope), s.max_repair_attempts,
            )
            for s in specs
        ]

        preview = compile_run_plan(
            feature="cutover-feature", definitions=preview_inputs, profile=profile)
        execute = compile_run_plan(
            feature="cutover-feature", definitions=execute_inputs, profile=profile)

        self.assertEqual(preview.selection, execute.selection)
        self.assertEqual(preview.order, execute.order)
        self.assertEqual(preview.adapter, execute.adapter)
        self.assertEqual(preview.control_records(), execute.control_records())
        for tid in ("DA-01", "DA-02"):
            p, e = preview.task(tid), execute.task(tid)
            self.assertEqual(str(p.working_root), str(e.working_root))
            self.assertEqual(str(p.storage_root), str(e.storage_root))
            self.assertEqual(
                [(c.name, c.argv) for c in p.checks],
                [(c.name, c.argv) for c in e.checks],
            )
            self.assertEqual(int(p.repair_bound), int(e.repair_bound))

    def test_dry_run_renders_a_fail_closed_transition_for_an_unroutable_type(self) -> None:
        # No compiled plan is built when a selected type has no route; the preview still
        # renders C1-C8, now with a fail-closed C2/C6 line and exit 30.
        with TemporaryDirectory() as directory:
            seed = _seed(
                directory,
                plan_tasks=[
                    {"id": "DA-01", "type": "docs"},
                    {"id": "MY-02", "type": "mystery"},
                ],
                profile=_native_profile(),
            )
            code, out, err = _run(seed["anchors"] + [
                "--profile", "profile.json", "--plan", "plan.json", "--dry-run",
            ])
        self.assertEqual(code, 30, err)
        self.assertIn("MY-02: unknown-task-type (type mystery)", out)
        self.assertIn("MY-02: pending -> blocked (unknown-task-type)", out)
        self.assertIn("C5. Exit code: 30", out)
        self.assertEqual(list(seed["root"].rglob("run.json")), [])

    def test_dry_run_c2_routing_is_rendered_from_the_compiled_plan(self) -> None:
        with TemporaryDirectory() as directory:
            seed = _seed(
                directory,
                plan_tasks=[{"id": "DA-01", "type": "docs"}],
                profile=_native_profile(),
            )
            code, out, err = _run(seed["anchors"] + [
                "--profile", "profile.json", "--plan", "plan.json", "--dry-run",
            ])
        self.assertEqual(code, 10, err)
        profile = compiled_profile_from_core(Profile.from_data(_native_profile()))
        task = compile_run_plan(
            feature="cutover-feature",
            definitions=[ShallowTaskInput("DA-01", "docs")],
            profile=profile,
        ).task("DA-01")
        self.assertIn(
            f"DA-01: stack=text root=<project_root>/{task.working_root} "
            f"storage=<project_root>/{task.storage_root} checks=[lint] subagents=[executor]",
            out,
        )


# --- AC-2 - fail closed before any run state ------------------------------------------


class RejectBeforeStateTests(unittest.TestCase):
    def test_unroutable_task_type_in_execute_is_rejected_before_run_state(self) -> None:
        with TemporaryDirectory() as directory:
            seed = _seed(
                directory,
                plan_tasks=[
                    {"id": "DA-01", **_FULL_JSON_TASK},
                    {"id": "MY-02", **{**_FULL_JSON_TASK, "task_type": "mystery"}},
                ],
                profile=_native_profile(),
            )
            code, _out, err = _run(seed["anchors"] + [
                "--profile", "profile.json", "--plan", "plan.json",
                "--mode", "execute", "--approve-plan",
            ])
        self.assertEqual(code, 30)
        self.assertIn("unknown task type", err.lower())
        self.assertEqual(list(seed["root"].rglob("run.json")), [])

    def test_design_type_is_rejected_before_run_state(self) -> None:
        # ``design`` is a recognised task type but has no default route (it needs an
        # explicit executor); the compiled-plan cutover must still fail it closed.
        with TemporaryDirectory() as directory:
            seed = _seed(
                directory,
                plan_tasks=[{"id": "DS-01", **{**_FULL_JSON_TASK, "task_type": "design"}}],
                profile=_native_profile(),
            )
            code, _out, err = _run(seed["anchors"] + [
                "--profile", "profile.json", "--plan", "plan.json",
                "--mode", "execute", "--approve-plan",
            ])
        self.assertEqual(code, 30)
        self.assertIn("unresolved-executor", err)
        self.assertEqual(list(seed["root"].rglob("run.json")), [])

    def test_selection_spanning_two_storage_roots_is_rejected_before_run_state(self) -> None:
        with TemporaryDirectory() as directory:
            seed = _seed(
                directory,
                plan_tasks=[
                    {"id": "DA-01", **_FULL_JSON_TASK},
                    {"id": "PY-02", **{**_FULL_JSON_TASK, "task_type": "python",
                                       "executor": "python-executor",
                                       "allowed_scope": ["code/**"]}},
                ],
                profile=_native_profile(two_storage=True),
            )
            code, _out, err = _run(seed["anchors"] + [
                "--profile", "profile.json", "--plan", "plan.json",
                "--mode", "execute", "--approve-plan",
            ])
        self.assertEqual(code, 30)
        self.assertIn("conflicting-task-storage", err)
        self.assertEqual(list(seed["root"].rglob("run.json")), [])


# --- AC-3 - resume reports field-level plan incompatibility --------------------------


class ResumeCompatibilityTests(unittest.TestCase):
    def _plan(self, *, max_repair: int | None = None):
        profile = compiled_profile_from_core(Profile.from_data(_native_profile()))
        return compile_run_plan(
            feature="cutover-feature",
            definitions=[ShallowTaskInput("DA-01", "docs")],
            profile=profile,
            overrides=ControlOverrides(max_repair_attempts=max_repair),
        )

    def test_fingerprint_is_deterministic_and_field_addressable(self) -> None:
        one, two = _plan_fingerprint(self._plan()), _plan_fingerprint(self._plan())
        self.assertEqual(one, two)
        self.assertIn("task.DA-01.working_root", one)
        self.assertIn("task.DA-01.repair_bound", one)
        self.assertIn("adapter", one)

    def test_resume_with_a_changed_repair_bound_names_the_field(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = Run.create(
                "cutover-feature", root / "prompt.md", root / "plan.json",
                root / "runs" / "cutover-feature", root,
            )
            recorded = self._plan(max_repair=2)
            run.set_control(
                "plan_fingerprint", _plan_fingerprint(recorded), sourced="explicit")
            run.set_control("plan_digest", recorded.digest, sourced="explicit")
            run.save()
            before = (run.run_dir / "run.json").read_bytes()

            with self.assertRaises(ExecutionError) as caught:
                _ensure_plan_compatible(run, self._plan(max_repair=5))
            self.assertEqual(caught.exception.code, "plan-incompatible")
            self.assertIn("repair_bound", str(caught.exception))
            self.assertEqual((run.run_dir / "run.json").read_bytes(), before)

    def test_resume_with_the_same_plan_is_accepted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = Run.create(
                "cutover-feature", root / "prompt.md", root / "plan.json",
                root / "runs" / "cutover-feature", root,
            )
            plan = self._plan()
            run.set_control(
                "plan_fingerprint", _plan_fingerprint(plan), sourced="explicit")
            run.save()
            _ensure_plan_compatible(run, plan)  # no raise


if __name__ == "__main__":
    unittest.main()
