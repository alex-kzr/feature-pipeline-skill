"""TC-08 — the canonical ``stacks[]`` binding (``{id, role, checks}``) all loaders agree on.

TC-07 fixed the first-stack/all-checks defect and gave every route/check its own explicit
``stack``, but the setup generator, the runnable project-profile loader, and the typed profile
loader still never required or validated a single generated ``stacks[]`` registry. TC-08 closes
that gap: every route's stack must resolve through a declared ``{id, role, checks}`` entry, the
role must be one of the profile's own declared ``roles[]`` names, every ``checks.json`` check
must be claimed by exactly one stack whose own declared ``stack`` field agrees, and a profile
generated before this binding existed is rejected with an explicit, distinct diagnostic rather
than silently tolerated or patched.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from feature_pipeline.application.profile_bridge import compiled_profile_from_core
from feature_pipeline.contracts import SchemaError
from feature_pipeline.inputs.profile import CompiledProfile, InvalidProfile, UnknownStack
from pipeline_core.project_profile import load_runnable_profile

FIXTURES = Path(__file__).parent / "fixtures" / "task_model_routing"
GENERATED = FIXTURES / "generated" / "pipeline.profile.json"


def _generated() -> tuple[dict, dict]:
    profile = json.loads(GENERATED.read_text(encoding="utf-8"))
    checks = json.loads((GENERATED.parent / "checks.json").read_text(encoding="utf-8"))
    return profile, checks


def _write_and_load_native(profile: dict, checks: dict):
    with TemporaryDirectory() as directory:
        path = Path(directory) / "pipeline.profile.json"
        path.write_text(json.dumps(profile), encoding="utf-8")
        (path.parent / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
        return load_runnable_profile(path)


class StacksBindingPositiveTests(unittest.TestCase):
    def test_typed_and_native_expose_the_same_stack_bindings(self) -> None:
        raw, checks = _generated()
        typed = CompiledProfile.from_mapping(raw, checks)
        native = load_runnable_profile(GENERATED)
        bridged = compiled_profile_from_core(native)

        for stack_id, expected_role, expected_checks in (
            ("python", "executor", ("python-tests",)),
            ("rust", "executor", ("rust-tests",)),
            ("docs", "executor", ()),
            ("repo", "executor", ("repo-check",)),
        ):
            with self.subTest(stack_id=stack_id):
                binding = typed.stack_for(stack_id)
                self.assertEqual(binding.role, expected_role)
                self.assertEqual(binding.check_names, expected_checks)
                bridged_binding = bridged.stack_for(stack_id)
                self.assertEqual(bridged_binding.role, expected_role)

    def test_unknown_stack_lookup_fails_closed(self) -> None:
        typed = CompiledProfile.from_mapping(*_generated())
        with self.assertRaises(UnknownStack):
            typed.stack_for("borg")


class StacksBindingDenialTests(unittest.TestCase):
    def test_missing_stacks_key_is_a_distinct_legacy_diagnostic(self) -> None:
        raw, checks = _generated()
        del raw["stacks"]
        with self.assertRaisesRegex(InvalidProfile, "stacks"):
            CompiledProfile.from_mapping(raw, checks)
        with self.assertRaisesRegex(SchemaError, "stacks"):
            _write_and_load_native(raw, checks)

    def test_duplicate_stack_id_is_rejected(self) -> None:
        raw, checks = _generated()
        raw["stacks"].append(copy.deepcopy(raw["stacks"][0]))
        with self.assertRaisesRegex(InvalidProfile, "duplicate"):
            CompiledProfile.from_mapping(raw, checks)
        with self.assertRaisesRegex(SchemaError, "duplicate"):
            _write_and_load_native(raw, checks)

    def test_unknown_stack_role_is_rejected(self) -> None:
        raw, checks = _generated()
        raw["stacks"][0]["role"] = "borg"
        with self.assertRaisesRegex(InvalidProfile, "role"):
            CompiledProfile.from_mapping(raw, checks)
        with self.assertRaisesRegex(SchemaError, "role"):
            _write_and_load_native(raw, checks)

    def test_stack_claiming_an_undeclared_check_is_rejected(self) -> None:
        raw, checks = _generated()
        raw["stacks"][0]["checks"].append("no-such-check")
        with self.assertRaisesRegex(InvalidProfile, "not declared in checks.json"):
            CompiledProfile.from_mapping(raw, checks)
        with self.assertRaisesRegex(SchemaError, "not declared in checks.json"):
            _write_and_load_native(raw, checks)

    def test_stack_check_id_mismatch_is_rejected(self) -> None:
        raw, checks = _generated()
        # "python-tests" is claimed by stacks[0] ("python"), but checks.json now disagrees.
        for entry in checks["checks"]:
            if entry["name"] == "python-tests":
                entry["stack"] = "rust"
        with self.assertRaisesRegex(InvalidProfile, "declares stack"):
            CompiledProfile.from_mapping(raw, checks)
        with self.assertRaisesRegex(SchemaError, "declares stack"):
            _write_and_load_native(raw, checks)

    def test_orphaned_check_not_claimed_by_any_stack_is_rejected(self) -> None:
        raw, checks = _generated()
        checks["checks"].append(
            {"name": "orphan", "stack": "python", "argv": ["true"], "cwd": "."}
        )
        with self.assertRaisesRegex(InvalidProfile, "not claimed by any"):
            CompiledProfile.from_mapping(raw, checks)
        with self.assertRaisesRegex(SchemaError, "not claimed by any"):
            _write_and_load_native(raw, checks)

    def test_route_stack_not_declared_in_stacks_is_rejected(self) -> None:
        raw, checks = _generated()
        raw["task_routing"][0]["stack"] = "unmapped"
        with self.assertRaisesRegex(InvalidProfile, "not declared in this profile's stacks"):
            CompiledProfile.from_mapping(raw, checks)
        with self.assertRaisesRegex(SchemaError, "not declared in this profile's stacks"):
            _write_and_load_native(raw, checks)


if __name__ == "__main__":
    unittest.main()
