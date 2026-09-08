"""TC-01: observed route/check contracts, including the separate task command path.

These passing characterizations describe the baseline, including representation loss.
Change the expectations deliberately when the later routing tasks change that contract.
All profiles are portable fixtures; no compiler fixture invokes its declared tools.
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
from feature_pipeline.contracts import TaskSpec
from feature_pipeline.inputs.profile import CompiledProfile, InvalidProfile
from feature_pipeline.ports.process import ProcessOutcome
from pipeline_core.profiles import Anchors, resolve_route
from pipeline_core.project_profile import load_runnable_profile
from pipeline_core.verification import VerifierAnchors, VerifierLaunchers
from tests.support.builders import initialize_run


FIXTURES = Path(__file__).parent / "fixtures" / "task_model_routing"
GENERATED = FIXTURES / "generated" / "pipeline.profile.json"
CHECKS = ("python-tests", "repo-check", "rust-tests")


def _compile(profile: CompiledProfile):
    return compile_run_plan(
        feature="routing-baseline", profile=profile,
        definitions=tuple(ShallowTaskInput(f"BASE-{i}", kind)
                          for i, kind in enumerate(("python", "rust", "docs"), 1)),
    )


class RouteRepresentationCharacterizationTests(unittest.TestCase):
    def test_generated_loaders_assign_sorted_first_stack_and_all_checks(self) -> None:
        native = load_runnable_profile(GENERATED)
        typed = CompiledProfile.from_path(GENERATED)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for kind, working_root in (("python", "packages/python"),
                                       ("rust", "packages/rust"), ("docs", "docs")):
                with self.subTest(kind=kind):
                    resolved = resolve_route(native, kind, Anchors(root, root, root))
                    route = typed.route_for(kind)
                    self.assertEqual(resolved.route.stack, "python")
                    self.assertEqual(route.stack, "python")
                    self.assertEqual(tuple(resolved.route.checks), CHECKS)
                    self.assertEqual(route.check_names, CHECKS)
                    self.assertEqual(route.subagents, ("executor",))
                    self.assertEqual(resolved.root, root / working_root)
                    self.assertEqual(str(route.working_root), working_root)

    def test_direct_typed_checks_keep_stack_cwd_while_native_bridge_loses_them(self) -> None:
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
                self.assertEqual((bridge.checks[name].stack, str(bridge.checks[name].cwd)), ("", "."))
                self.assertEqual(bridge.checks[name].argv, direct.checks[name].argv)

    def test_compiler_keeps_input_cwd_but_resolved_checks_have_no_stack_field(self) -> None:
        for loader, cwds in (
            (lambda: CompiledProfile.from_path(GENERATED), ("packages/python", ".", "packages/rust")),
            (lambda: compiled_profile_from_core(load_runnable_profile(GENERATED)), (".", ".", ".")),
        ):
            with self.subTest(cwds=cwds):
                plan = _compile(loader())
                for task in plan.tasks:
                    self.assertEqual(tuple(check.name for check in task.checks), CHECKS)
                    self.assertEqual(tuple(str(check.cwd) for check in task.checks), cwds)
                    self.assertTrue(all(not hasattr(check, "stack") for check in task.checks))

    def test_native_routes_retain_distinct_stacks_workers_and_check_subsets(self) -> None:
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
                self.assertTrue(all(str(check.cwd) == "." for check in task.checks))

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
