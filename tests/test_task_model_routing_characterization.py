"""TC-01/TC-07: observed route/check contracts, including the separate task command path.

TC-01 characterized the baseline defect: every generated-profile route was assigned the
first sorted stack and every declared check, and the native-to-typed bridge rebuilt every
check with ``stack=""``/``cwd="."``. TC-07 fixes both: a profile-declared route now carries
its own explicit stack, resolves only that stack's checks plus checks explicitly marked
``required``, and every conversion path (native, typed, bridged) preserves the same check
stack/argv/cwd identity. All profiles are portable fixtures; no compiler fixture invokes its
declared tools.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from feature_pipeline.application.compile_plan import ShallowTaskInput, compile_run_plan
from feature_pipeline.application.profile_bridge import compiled_profile_from_core, route_reasons
from feature_pipeline.application.verification_service import VerificationRequest, VerificationService
from feature_pipeline.contracts import SchemaError, TaskSpec
from feature_pipeline.inputs.profile import CompiledProfile, InvalidProfile
from feature_pipeline.ports.process import ProcessOutcome
from pipeline_core.profiles import Anchors, resolve_route
from pipeline_core.project_profile import load_runnable_profile
from pipeline_core.verification import VerifierAnchors, VerifierLaunchers
from tests.support.builders import initialize_run


FIXTURES = Path(__file__).parent / "fixtures" / "task_model_routing"
GENERATED = FIXTURES / "generated" / "pipeline.profile.json"
#: ``repo-check`` is declared ``"required": true`` in the generated fixture's checks.json, so
#: every route below carries it in addition to its own stack-matched check(s) (AC-1/AC-3).
ROUTE_STACKS_AND_CHECKS = {
    "python": ("python", ("python-tests", "repo-check"), "packages/python"),
    "rust": ("rust", ("repo-check", "rust-tests"), "packages/rust"),
    "docs": ("docs", ("repo-check",), "docs"),
}


def _write_and_load(profile: dict, checks: dict):
    """Round-trip a mutated generated profile + checks.json through the on-disk native loader."""
    with TemporaryDirectory() as directory:
        path = Path(directory) / "pipeline.profile.json"
        path.write_text(json.dumps(profile), encoding="utf-8")
        (path.parent / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
        return load_runnable_profile(path)


def _compile(profile: CompiledProfile):
    return compile_run_plan(
        feature="routing-baseline", profile=profile,
        definitions=tuple(ShallowTaskInput(f"BASE-{i}", kind)
                          for i, kind in enumerate(("python", "rust", "docs"), 1)),
    )


class RouteRepresentationCharacterizationTests(unittest.TestCase):
    def test_generated_loaders_bind_each_route_to_its_own_stack_and_matching_checks(self) -> None:
        """TC-07 fix: a route carries its declared stack, resolving only that stack's checks
        plus any check explicitly marked ``required`` — never every declared check nor the
        first sorted stack (the TC-01 defect)."""
        native = load_runnable_profile(GENERATED)
        typed = CompiledProfile.from_path(GENERATED)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for kind, (stack, checks, working_root) in ROUTE_STACKS_AND_CHECKS.items():
                with self.subTest(kind=kind):
                    resolved = resolve_route(native, kind, Anchors(root, root, root))
                    route = typed.route_for(kind)
                    self.assertEqual(resolved.route.stack, stack)
                    self.assertEqual(route.stack, stack)
                    self.assertEqual(tuple(resolved.route.checks), checks)
                    self.assertEqual(route.check_names, checks)
                    self.assertEqual(route.subagents, ("executor",))
                    self.assertEqual(resolved.root, root / working_root)
                    self.assertEqual(str(route.working_root), working_root)

    def test_direct_typed_checks_and_native_bridge_agree_on_stack_and_cwd(self) -> None:
        """TC-07 fix: the native-to-typed bridge no longer rebuilds every check with
        ``stack=""``/``cwd="."`` — it now carries the same identity the profile declared,
        directly parsed."""
        direct = CompiledProfile.from_path(GENERATED)
        bridge = compiled_profile_from_core(load_runnable_profile(GENERATED))
        expected = {
            "python-tests": ("python", "packages/python"),
            "rust-tests": ("rust", "packages/rust"),
            "repo-check": ("repo", "."),
        }
        for name, identity in expected.items():
            with self.subTest(check=name):
                self.assertEqual((direct.checks[name].stack, str(direct.checks[name].cwd)), identity)
                self.assertEqual((bridge.checks[name].stack, str(bridge.checks[name].cwd)), identity)
                self.assertEqual(bridge.checks[name].argv, direct.checks[name].argv)

    def test_compiler_preserves_stack_and_cwd_and_only_the_routes_own_checks(self) -> None:
        """TC-07 fix: ``ResolvedCheck`` carries the profile's stack, and a task only ever
        gets its own route's checks — the typed and native-bridged paths now agree exactly
        (AC-2/AC-3): unrelated stacks' checks are not silently included."""
        expected = {
            "python": (("python-tests", "python", "packages/python"), ("repo-check", "repo", ".")),
            "rust": (("repo-check", "repo", "."), ("rust-tests", "rust", "packages/rust")),
            "docs": (("repo-check", "repo", "."),),
        }
        for loader in (
            lambda: CompiledProfile.from_path(GENERATED),
            lambda: compiled_profile_from_core(load_runnable_profile(GENERATED)),
        ):
            with self.subTest(loader=loader):
                plan = _compile(loader())
                for task in plan.tasks:
                    got = tuple((check.name, check.stack, str(check.cwd)) for check in task.checks)
                    self.assertEqual(got, expected[task.task_type])

    def test_native_routes_retain_distinct_stacks_workers_and_check_subsets(self) -> None:
        """Regression coverage for the legacy shape: a hand-authored native profile that
        explicitly curates each route's checks (rather than declaring per-check stacks in
        ``registry.checks``) keeps working unchanged — its checks carry ``stack=""``."""
        profile = load_runnable_profile(FIXTURES / "native.json")
        bridge = compiled_profile_from_core(profile)
        expected = {
            "python": ("python", ("python-tests", "repo-check")),
            "rust": ("rust", ("rust-tests", "repo-check")),
            "docs": ("docs", ("repo-check",)),
        }
        plan = _compile(bridge)
        for task in plan.tasks:
            with self.subTest(kind=task.task_type):
                route = bridge.route_for(task.task_type)
                self.assertEqual((route.stack, route.check_names), expected[task.task_type])
                self.assertEqual(route.subagents, (f"{task.task_type}-worker",))
                self.assertEqual(tuple(check.name for check in task.checks), route.check_names)
                self.assertTrue(all(check.stack == "" for check in task.checks))
                self.assertTrue(all(str(check.cwd) == "." for check in task.checks))

    def test_missing_or_invalid_route_stack_is_rejected(self) -> None:
        """AC-2: a route with no declared stack, or an empty one, fails closed on both the
        native and typed conversion paths — a route can never fall back to guessing a stack."""
        raw = json.loads(GENERATED.read_text(encoding="utf-8"))
        checks = json.loads((GENERATED.parent / "checks.json").read_text(encoding="utf-8"))
        for mutate in (
            lambda routing: routing.pop("stack"),
            lambda routing: routing.__setitem__("stack", ""),
        ):
            with self.subTest(mutate=mutate):
                mutated = json.loads(json.dumps(raw))
                mutate(mutated["task_routing"][0])
                with self.assertRaises(SchemaError):
                    _write_and_load(mutated, checks)
                with self.assertRaises(InvalidProfile):
                    CompiledProfile.from_mapping(mutated, checks)

    def test_route_stack_with_no_matching_or_required_checks_is_rejected(self) -> None:
        """AC-3: a route may not silently resolve to zero checks — an unrelated stack's
        checks are never substituted, and a route with no stack match and no required check
        fails closed instead of dispatching with nothing verified."""
        raw = json.loads(GENERATED.read_text(encoding="utf-8"))
        checks = json.loads((GENERATED.parent / "checks.json").read_text(encoding="utf-8"))
        # No check in this fixture is stack-unmatched *and* required at once; drop the
        # required flag so an unmapped stack truly resolves nothing, rather than falling
        # back to the repo-wide required check.
        for entry in checks["checks"]:
            entry.pop("required", None)
        raw["task_routing"][0]["stack"] = "unmapped-stack"
        with self.assertRaises(SchemaError):
            _write_and_load(raw, checks)
        with self.assertRaises(InvalidProfile):
            CompiledProfile.from_mapping(raw, checks)

    def test_unknown_and_unrouted_types_remain_distinct_denials(self) -> None:
        profile = load_runnable_profile(FIXTURES / "native.json")
        self.assertEqual(route_reasons(profile, ("python", "invented", "design", "tooling")), {
            "invented": "unknown-task-type", "design": "unresolved-executor",
            "tooling": "unresolved-executor",
        })
        raw = json.loads(GENERATED.read_text(encoding="utf-8"))
        raw["schema_version"] = 99
        with self.assertRaises(InvalidProfile):
            CompiledProfile.from_mapping(raw)


class TaskCommandPathCharacterizationTests(unittest.TestCase):
    def test_verification_service_runs_task_commands_in_their_own_cwds(self) -> None:
        commands = (
            {"cwd": "packages/rust", "argv": ["fixture-check", "rust"]},
            {"cwd": "packages/python", "argv": ["fixture-check", "python"]},
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for command in commands:
                (root / command["cwd"]).mkdir(parents=True)
            life = initialize_run(root, tasks=(("BASE-1", ()),))
            spec = TaskSpec.build(id="BASE-1", task_type="rust", executor="executor",
                                  allowed_scope=("packages/**",), verification_commands=commands)
            with (
                patch("pipeline_core.commands.resolve_program", return_value="fixture-check"),
                patch("pipeline_core.commands.LocalProcessRunner.run",
                      side_effect=(ProcessOutcome(1), ProcessOutcome(0))) as process,
                patch("feature_pipeline.application.verification_service.orchestrate_verification"),
            ):
                VerificationService().verify(life.run, VerificationRequest(
                    spec=spec, launchers=VerifierLaunchers(task=None, test=None),
                    anchors=VerifierAnchors(project_root=str(root), agents_root="shared"), attempt=1,
                ))
            launched = [call.args[0] for call in process.call_args_list]
            self.assertEqual([Path(item.cwd) for item in launched],
                             [(root / command["cwd"]).resolve() for command in commands])
            self.assertEqual([item.argv for item in launched],
                             [tuple(command["argv"]) for command in commands])
            self.assertEqual([record["cwd"] for record in life.run.commands],
                             [command["cwd"] for command in commands])
            self.assertEqual([record["disposition"] for record in life.run.commands], ["FAIL", "PASS"])
            self.assertTrue(all("snapshot" not in record for record in life.run.commands))


if __name__ == "__main__":
    unittest.main()
