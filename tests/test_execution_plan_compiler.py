"""CP-01 — compile every execution decision once, before any side effect.

Plan: ``docs/plans/tasks/CP-01_execution-plan-compiler.md`` (AC-1, AC-2, AC-3),
``docs/adr/006`` (profiles own routes, one immutable compiled plan) and ``docs/adr/007`` §4/§5
(controls resolved and validated at the boundary).

The compiler builds :class:`feature_pipeline.domain.plan.CompiledRunPlan` from normalized
:class:`feature_pipeline.domain.models.TaskDefinition` inputs (IN-01), a typed
:class:`feature_pipeline.inputs.profile.CompiledProfile`, and an in-process
:class:`feature_pipeline.ports.adapters.AdapterRegistry`. It is a pure function: every denial
is proven here without a run directory, a lease file, an artifact, or a subprocess.

Nothing in ``pipeline_core`` changes — CP-02 cuts the production dry-run / execute / resume
paths over to this object. These tests pin parity of the resolved route against the shipped
``pipeline_core.profiles.resolve_route``.

Standard library only.
"""

from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path

from feature_pipeline.application.compile_plan import (
    DEFAULT_ROLE_GRANT,
    DIAGNOSTIC_OUTPUT_BUDGET,
    ROUTINE_OUTPUT_BUDGET,
    ControlOverrides,
    compile_run_plan,
)
from feature_pipeline.application.selection import SelectionError
from feature_pipeline.domain.controls import ControlSource
from feature_pipeline.domain.errors import DomainError, InvalidControl, InvalidIdentifier
from feature_pipeline.domain.graph import DependencyCycle, UnknownDependency
from feature_pipeline.domain.models import MARKDOWN_TASK_FILE, TaskDefinition
from feature_pipeline.domain.plan import (
    CompiledRunPlan,
    ConflictingStorage,
    ResolvedTask,
    UnsupportedControl,
)
from feature_pipeline.domain.paths import RelativePath
from feature_pipeline.inputs.profile import (
    CheckCommand,
    CompiledProfile,
    InvalidProfile,
    RoutePolicy,
    UnknownRoute,
)
from feature_pipeline.ports.adapters import (
    AdapterCapabilities,
    AdapterRegistry,
    AdapterUnavailable,
    IncompatibleAdapter,
    UnknownAdapter,
)

from feature_pipeline.contracts import TaskSpec
from pipeline_core.profiles import Anchors, resolve_route
from pipeline_core.project_profile import load_runnable_profile

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROFILE_PATH = _REPO_ROOT / "tools" / "feature-pipeline" / "config" / "pipeline.profile.json"


def _generated_profile_mapping() -> tuple[dict, dict]:
    profile = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
    checks = json.loads((_PROFILE_PATH.parent / "checks.json").read_text(encoding="utf-8"))
    return profile, checks


def _profile() -> CompiledProfile:
    raw, checks = _generated_profile_mapping()
    return CompiledProfile.from_mapping(raw, checks)


def _definition(
    task_id: str,
    *,
    task_type: str = "python",
    depends_on: tuple[str, ...] = (),
    max_repair_attempts: int = 2,
    allowed_scope: tuple[str, ...] = ("feature-pipeline-skill/tests/**",),
) -> TaskDefinition:
    spec = TaskSpec.build(
        id=task_id,
        task_type=task_type,
        executor="python-executor",
        allowed_scope=list(allowed_scope),
        depends_on=list(depends_on),
        max_repair_attempts=max_repair_attempts,
    )
    return TaskDefinition(spec=spec, source_format=MARKDOWN_TASK_FILE)


# --------------------------------------------------------------------------------------------
# AdapterRegistry
# --------------------------------------------------------------------------------------------


class AdapterRegistryTests(unittest.TestCase):
    def test_auto_resolves_to_the_first_available_adapter(self) -> None:
        cap = AdapterRegistry.default().resolve(None)
        self.assertEqual(cap.name, "claude")
        self.assertTrue(cap.available)
        self.assertTrue(cap.supports_resume)
        self.assertEqual(cap.default_timeout_s, 3600.0)

    def test_explicit_claude_resolves(self) -> None:
        self.assertEqual(AdapterRegistry.default().resolve("claude").name, "claude")

    def test_recognized_but_unavailable_adapter_fails_closed(self) -> None:
        with self.assertRaises(AdapterUnavailable):
            AdapterRegistry.default().resolve("codex")

    def test_unknown_adapter_is_rejected(self) -> None:
        with self.assertRaises(UnknownAdapter):
            AdapterRegistry.default().resolve("borg")

    def test_no_available_adapter_fails_closed(self) -> None:
        empty = AdapterRegistry(
            (AdapterCapabilities("claude", False, True, True, True, 3600.0),)
        )
        with self.assertRaises(AdapterUnavailable):
            empty.resolve(None)

    def test_plan_selection_keeps_a_declared_unavailable_adapter(self) -> None:
        registry = AdapterRegistry(
            (AdapterCapabilities("claude", False, True, True, True, 3600.0),)
        )
        self.assertEqual(registry.select(None).name, "claude")

    def test_plan_selection_prefers_an_available_adapter(self) -> None:
        registry = AdapterRegistry(
            (
                AdapterCapabilities("claude", False, True, True, True, 3600.0),
                AdapterCapabilities("codex", True, False, True, True, 3600.0),
            )
        )
        self.assertEqual(registry.select(None).name, "codex")

    def test_require_a_capability_the_adapter_lacks(self) -> None:
        registry = AdapterRegistry(
            (AdapterCapabilities("claude", True, False, True, True, 3600.0),)
        )
        with self.assertRaises(IncompatibleAdapter):
            registry.require("claude", ("resume",))

    def test_every_error_is_a_domain_error(self) -> None:
        for exc in (AdapterUnavailable, IncompatibleAdapter, UnknownAdapter):
            self.assertTrue(issubclass(exc, DomainError))


# --------------------------------------------------------------------------------------------
# CompiledProfile
# --------------------------------------------------------------------------------------------


class CompiledProfileTests(unittest.TestCase):
    def test_generated_profile_owns_every_declared_route(self) -> None:
        profile = _profile()
        self.assertEqual(profile.project, "feature-pipeline")
        self.assertEqual(
            sorted(profile.routes),
            ["agent_config", "docs", "python", "research", "tooling"],
        )
        route = profile.route_for("python")
        self.assertIsInstance(route, RoutePolicy)
        self.assertEqual(str(route.working_root), "feature-pipeline-skill")
        self.assertEqual(route.check_names, ("core-unittests", "whitespace-check"))
        self.assertEqual(route.subagents, ("executor",))

    def test_check_command_carries_argv_and_cwd(self) -> None:
        check = _profile().check_command("core-unittests")
        self.assertIsInstance(check, CheckCommand)
        self.assertEqual(
            check.argv,
            ("uv", "run", "python", "-m", "unittest", "discover", "-s", "tests", "-t", "."),
        )
        self.assertEqual(str(check.cwd), "feature-pipeline-skill")

    def test_unknown_route_fails_closed(self) -> None:
        with self.assertRaises(UnknownRoute):
            _profile().route_for("design")

    def test_unknown_check_fails_closed(self) -> None:
        with self.assertRaises(DomainError):
            _profile().check_command("nope")

    def test_unknown_schema_version_is_rejected(self) -> None:
        raw, checks = _generated_profile_mapping()
        raw["schema_version"] = 99
        with self.assertRaises(InvalidProfile):
            CompiledProfile.from_mapping(raw, checks)

    def test_native_core_key_is_rejected(self) -> None:
        raw, checks = _generated_profile_mapping()
        raw["registry"] = {}
        with self.assertRaises(InvalidProfile):
            CompiledProfile.from_mapping(raw, checks)

    def test_missing_required_key_is_rejected(self) -> None:
        raw, checks = _generated_profile_mapping()
        del raw["roles"]
        with self.assertRaises(InvalidProfile):
            CompiledProfile.from_mapping(raw, checks)

    def test_duplicate_task_type_is_rejected(self) -> None:
        raw, checks = _generated_profile_mapping()
        raw["task_routing"].append({"task_type": "python", "working_root": "elsewhere"})
        with self.assertRaises(InvalidProfile):
            CompiledProfile.from_mapping(raw, checks)

    def test_unsafe_anchor_is_rejected(self) -> None:
        raw, checks = _generated_profile_mapping()
        raw["anchors"]["core_root"] = "../escape"
        with self.assertRaises(DomainError):
            CompiledProfile.from_mapping(raw, checks)

    def test_from_path_matches_from_mapping(self) -> None:
        from_path = CompiledProfile.from_path(_PROFILE_PATH)
        self.assertEqual(from_path.project, _profile().project)
        self.assertEqual(sorted(from_path.routes), sorted(_profile().routes))


# --------------------------------------------------------------------------------------------
# compile_run_plan — happy path
# --------------------------------------------------------------------------------------------


class CompileRunPlanTests(unittest.TestCase):
    def test_compiles_an_immutable_plan_for_every_selected_task(self) -> None:
        plan = compile_run_plan(
            feature="feature-pipeline",
            definitions=[_definition("CP-01"), _definition("CP-02", depends_on=("CP-01",))],
            profile=_profile(),
        )
        self.assertIsInstance(plan, CompiledRunPlan)
        self.assertEqual(plan.selection, ("CP-01", "CP-02"))
        self.assertEqual(plan.selection_mode, "all")
        self.assertEqual(plan.order, ("CP-01", "CP-02"))
        self.assertEqual(plan.adapter, "claude")
        self.assertEqual([t.task_id for t in plan], ["CP-01", "CP-02"])

        task = plan.task("CP-01")
        self.assertIsInstance(task, ResolvedTask)
        self.assertEqual(str(task.working_root), "feature-pipeline-skill")
        self.assertEqual(str(task.storage_root), ".pipeline/runs")
        self.assertEqual(task.role, "executor")
        self.assertEqual(task.role_grant, ("read", "run_checks", "write"))
        self.assertEqual(task.adapter, "claude")
        self.assertEqual(task.timeout_s, 3600.0)
        self.assertEqual(task.routine_output_budget, ROUTINE_OUTPUT_BUDGET)
        self.assertEqual(task.diagnostic_output_budget, DIAGNOSTIC_OUTPUT_BUDGET)
        self.assertEqual(task.serialized_programs, ())
        self.assertEqual(int(task.repair_bound), 2)
        self.assertEqual([c.name for c in task.checks], ["core-unittests", "whitespace-check"])
        self.assertIn("resume", task.capabilities)

    def test_frozen_dataclass_cannot_be_mutated(self) -> None:
        plan = compile_run_plan(
            feature="demo", definitions=[_definition("AA-01")], profile=_profile()
        )
        with self.assertRaises(Exception):
            plan.tasks[0].timeout_s = 1.0  # type: ignore[misc]

    def test_every_accepted_control_appears_once_with_provenance(self) -> None:
        # AC-1
        plan = compile_run_plan(
            feature="demo",
            definitions=[_definition("AA-01")],
            profile=_profile(),
            overrides=ControlOverrides(max_repair_attempts=4),
        )
        names = [c.name for c in plan.controls]
        self.assertEqual(sorted(names), sorted(set(names)))
        self.assertIn("adapter_requested", names)
        self.assertIn("adapter_resolved", names)

        by_name = {c.name: c for c in plan.controls}
        self.assertEqual(by_name["adapter_resolved"].value, "claude")
        self.assertEqual(by_name["adapter_resolved"].source, ControlSource.DEFAULT)

        task_repair = plan.task("AA-01").control("max_repair_attempts")
        self.assertEqual(task_repair.value, 4)
        self.assertEqual(task_repair.source, ControlSource.EXPLICIT)

    def test_declared_repair_bound_survives_when_no_override(self) -> None:
        plan = compile_run_plan(
            feature="demo",
            definitions=[_definition("AA-01", max_repair_attempts=1)],
            profile=_profile(),
        )
        repair = plan.task("AA-01").control("max_repair_attempts")
        self.assertEqual(repair.value, 1)
        self.assertEqual(repair.source, ControlSource.DEFAULT)

    def test_selection_task_and_through(self) -> None:
        defs = [
            _definition("AA-01"),
            _definition("AA-02", depends_on=("AA-01",)),
            _definition("AA-03", depends_on=("AA-02",)),
        ]
        one = compile_run_plan(
            feature="demo", definitions=defs, profile=_profile(), task="AA-02"
        )
        self.assertEqual(one.selection, ("AA-02",))
        self.assertEqual(one.selection_mode, "task")

        through = compile_run_plan(
            feature="demo", definitions=defs, profile=_profile(), through="AA-02"
        )
        self.assertEqual(through.selection, ("AA-01", "AA-02"))
        self.assertEqual(through.selection_mode, "through")

    def test_current_builtin_task_types_route_through_the_default_profile(self) -> None:
        # AC-3
        profile = _profile()
        defs = [
            _definition("PY-01", task_type="python"),
            _definition("TO-01", task_type="tooling"),
            _definition("AG-01", task_type="agent_config"),
            _definition("DO-01", task_type="docs"),
            _definition("RE-01", task_type="research"),
        ]
        plan = compile_run_plan(feature="demo", definitions=defs, profile=profile)
        roots = {t.task_id: str(t.working_root) for t in plan}
        self.assertEqual(
            roots,
            {
                "PY-01": "feature-pipeline-skill",
                "TO-01": ".",
                "AG-01": ".agents",
                "DO-01": "docs",
                "RE-01": "docs",
            },
        )

    def test_digest_is_deterministic_and_selection_sensitive(self) -> None:
        defs = [_definition("AA-01"), _definition("AA-02", depends_on=("AA-01",))]
        a = compile_run_plan(feature="demo", definitions=defs, profile=_profile())
        b = compile_run_plan(feature="demo", definitions=defs, profile=_profile())
        c = compile_run_plan(
            feature="demo", definitions=defs, profile=_profile(), task="AA-01"
        )
        self.assertEqual(a.digest, b.digest)
        self.assertNotEqual(a.digest, c.digest)
        self.assertRegex(a.digest, r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------------------------
# compile_run_plan — denials, all before any side effect (AC-2)
# --------------------------------------------------------------------------------------------


class CompileRunPlanDenialTests(unittest.TestCase):
    def test_invalid_feature_slug(self) -> None:
        with self.assertRaises(InvalidIdentifier):
            compile_run_plan(
                feature="bad slug", definitions=[_definition("AA-01")], profile=_profile()
            )

    def test_missing_route_is_rejected(self) -> None:
        with self.assertRaises(UnknownRoute):
            compile_run_plan(
                feature="demo",
                definitions=[_definition("AA-01", task_type="design")],
                profile=_profile(),
            )

    def test_unknown_dependency_is_rejected(self) -> None:
        with self.assertRaises(UnknownDependency):
            compile_run_plan(
                feature="demo",
                definitions=[_definition("AA-01", depends_on=("ZZ-99",))],
                profile=_profile(),
            )

    def test_dependency_cycle_is_rejected(self) -> None:
        # a self-free two-node cycle
        d1 = _definition("AA-01", depends_on=("AA-02",))
        d2 = _definition("AA-02", depends_on=("AA-01",))
        with self.assertRaises(DependencyCycle):
            compile_run_plan(feature="demo", definitions=[d1, d2], profile=_profile())

    def test_task_and_through_together_is_rejected(self) -> None:
        with self.assertRaises(SelectionError):
            compile_run_plan(
                feature="demo",
                definitions=[_definition("AA-01")],
                profile=_profile(),
                task="AA-01",
                through="AA-01",
            )

    def test_conflicting_task_storage_is_rejected(self) -> None:
        profile = CompiledProfile(
            project="demo",
            agents_root=RelativePath.parse(".agents"),
            core_root=RelativePath.parse("core"),
            run_state_path=RelativePath.parse(".pipeline/runs"),
            routes={
                "python": RoutePolicy(
                    "python", RelativePath.parse("core"), "python",
                    ("executor",), ("core-unittests",), "run_state",
                ),
                "docs": RoutePolicy(
                    "docs", RelativePath.parse("docs"), "python",
                    ("executor",), ("core-unittests",), "docs_state",
                ),
            },
            role_grants={"executor": ("read", "write")},
            checks={
                "core-unittests": CheckCommand(
                    "core-unittests", "python", ("true",), RelativePath.parse(".")
                )
            },
            storage={
                "run_state": RelativePath.parse(".pipeline/runs"),
                "docs_state": RelativePath.parse("docs/.state"),
            },
        )
        with self.assertRaises(ConflictingStorage):
            compile_run_plan(
                feature="demo",
                definitions=[
                    _definition("AA-01", task_type="python"),
                    _definition("DO-01", task_type="docs"),
                ],
                profile=profile,
            )

    def test_negative_repair_control_is_rejected_at_compile_time(self) -> None:
        with self.assertRaises(InvalidControl):
            compile_run_plan(
                feature="demo",
                definitions=[_definition("AA-01")],
                profile=_profile(),
                overrides=ControlOverrides(max_repair_attempts=-1),
            )

    def test_non_positive_byte_budget_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedControl):
            compile_run_plan(
                feature="demo",
                definitions=[_definition("AA-01")],
                profile=_profile(),
                overrides=ControlOverrides(routine_output_byte_budget=0),
            )

    def test_non_positive_timeout_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedControl):
            compile_run_plan(
                feature="demo",
                definitions=[_definition("AA-01")],
                profile=_profile(),
                overrides=ControlOverrides(timeout_s=-5.0),
            )

    def test_incompatible_adapter_requirement_is_rejected(self) -> None:
        registry = AdapterRegistry(
            (AdapterCapabilities("claude", True, False, True, True, 3600.0),)
        )
        with self.assertRaises(IncompatibleAdapter):
            compile_run_plan(
                feature="demo",
                definitions=[_definition("AA-01")],
                profile=_profile(),
                adapters=registry,
                required_adapter_capabilities=("resume",),
            )

    def test_explicit_unavailable_adapter_is_rejected(self) -> None:
        with self.assertRaises(AdapterUnavailable):
            compile_run_plan(
                feature="demo",
                definitions=[_definition("AA-01")],
                profile=_profile(),
                overrides=ControlOverrides(adapter="codex"),
            )

    def test_compiler_touches_no_effectful_boundary(self) -> None:
        src = inspect.getsource(compile_run_plan)
        for forbidden in ("subprocess", "pipeline_lease", "RunLifecycle", "open(", "mkdir"):
            self.assertNotIn(forbidden, src)


# --------------------------------------------------------------------------------------------
# parity with the shipped pipeline_core route resolution
# --------------------------------------------------------------------------------------------


class CompiledPlanSurfaceTests(unittest.TestCase):
    def _plan(self) -> CompiledRunPlan:
        return compile_run_plan(
            feature="demo",
            definitions=[_definition("AA-01")],
            profile=_profile(),
            overrides=ControlOverrides(
                routine_output_byte_budget=2048, timeout_s=120.0
            ),
        )

    def test_control_records_are_the_run_json_shape(self) -> None:
        records = self._plan().control_records()
        self.assertEqual(
            records["routine_output_byte_budget"],
            {"value": 2048, "sourced": "explicit"},
        )
        self.assertEqual(
            records["timeout_s"], {"value": 120.0, "sourced": "explicit"}
        )
        self.assertEqual(
            records["adapter_resolved"], {"value": "claude", "sourced": "default"}
        )

    def test_run_level_control_lookup_and_miss(self) -> None:
        plan = self._plan()
        self.assertEqual(plan.control("timeout_s").value, 120.0)
        with self.assertRaises(KeyError):
            plan.control("no-such-control")

    def test_resolved_task_control_miss(self) -> None:
        task = self._plan().task("AA-01")
        with self.assertRaises(KeyError):
            task.control("timeout_s")  # timeout is a run-level control, not per-task

    def test_task_lookup_miss(self) -> None:
        with self.assertRaises(KeyError):
            self._plan().task("ZZ-99")


class CompiledProfileDenialBranchTests(unittest.TestCase):
    def test_task_routing_must_be_a_list(self) -> None:
        raw, checks = _generated_profile_mapping()
        raw["task_routing"] = {"python": "x"}
        with self.assertRaises(InvalidProfile):
            CompiledProfile.from_mapping(raw, checks)

    def test_empty_min_grants_is_rejected(self) -> None:
        raw, checks = _generated_profile_mapping()
        raw["roles"][0]["min_grants"] = []
        with self.assertRaises(InvalidProfile):
            CompiledProfile.from_mapping(raw, checks)

    def test_checks_json_wrong_schema_version_is_rejected(self) -> None:
        raw, checks = _generated_profile_mapping()
        checks["schema_version"] = 7
        with self.assertRaises(InvalidProfile):
            CompiledProfile.from_mapping(raw, checks)

    def test_check_with_empty_argv_is_rejected(self) -> None:
        raw, checks = _generated_profile_mapping()
        checks["checks"][0]["argv"] = []
        with self.assertRaises(InvalidProfile):
            CompiledProfile.from_mapping(raw, checks)

    def test_no_checks_at_all_is_rejected(self) -> None:
        raw, checks = _generated_profile_mapping()
        checks["checks"] = []
        with self.assertRaises(InvalidProfile):
            CompiledProfile.from_mapping(raw, checks)

    def test_storage_root_unknown_key(self) -> None:
        with self.assertRaises(InvalidProfile):
            _profile().storage_root("nope")


class RouteParityTests(unittest.TestCase):
    def test_resolved_route_matches_pipeline_core_resolve_route(self) -> None:
        core_profile = load_runnable_profile(_PROFILE_PATH)
        anchors = Anchors(
            project_root=_REPO_ROOT,
            agents_root=_REPO_ROOT / ".agents",
            core_root=_REPO_ROOT / "feature-pipeline-skill",
        )
        # `agent_config` routes to `.agents`, which on a dev checkout is often a junction to
        # a Syncthing tree outside the repo — `resolve_route` rejects that anchor escape. The
        # compiled profile keeps the route logical, so AC-3's routing test covers it instead.
        for task_type in ("python", "tooling", "docs", "research"):
            shipped = resolve_route(core_profile, task_type, anchors)
            plan = compile_run_plan(
                feature="demo",
                definitions=[_definition("AA-01", task_type=task_type)],
                profile=_profile(),
            )
            task = plan.task("AA-01")
            shipped_root_tail = shipped.root.resolve().relative_to(_REPO_ROOT.resolve())
            self.assertEqual(str(task.working_root).replace("/", "\\"),
                             str(shipped_root_tail).replace("/", "\\"))
            shipped_storage_tail = shipped.storage.resolve().relative_to(_REPO_ROOT.resolve())
            self.assertEqual(str(task.storage_root).replace("/", "\\"),
                             str(shipped_storage_tail).replace("/", "\\"))
            self.assertEqual(
                tuple(c.name for c in task.checks), tuple(shipped.route.checks)
            )
            self.assertEqual(
                tuple(tuple(c.argv) for c in task.checks), tuple(shipped.checks)
            )


if __name__ == "__main__":
    unittest.main()
