"""Profile registry, explicit-anchor, and role-policy tests."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pipeline_core.profiles import Anchors, resolve_paths, resolve_route
from pipeline_core.roles import compose_role
from schemas import Profile, SchemaError


def profile_data() -> dict[str, object]:
    return {
        "version": 1,
        "name": "portable-example",
        "logical_paths": {"project": "workspace", "agents": "shared", "core": "core"},
        "role_grants": {
            "executor": ["read", "write", "run_checks"],
            "task_verifier": ["read"],
            "test_verifier": ["read", "run_checks"],
        },
        "stages": [{"name": "implement", "subagents": ["executor"], "argv": ["run"]}],
        "registry": {
            "task_types": {
                "python": {"stack": "python", "subagents": ["worker"], "root": "app", "checks": ["unit"], "storage": "runs"},
                "docs": {"stack": "docs", "subagents": ["writer"], "root": "docs", "checks": ["spell"], "storage": "reports"},
            },
            "stacks": {"python": {"runtime": "python"}, "docs": {"runtime": "text"}},
            "subagents": {"worker": {"role": "executor"}, "writer": {"role": "executor"}},
            "roots": {"app": "app", "docs": "docs"},
            "checks": {"unit": ["python", "-m", "unittest"], "spell": ["spellcheck"]},
            "storage": {"runs": ".pipeline/runs", "reports": ".pipeline/reports"},
        },
    }


class ProfileRegistryTests(unittest.TestCase):
    def test_distinct_registry_routes_are_resolved_without_project_branching(self) -> None:
        profile = Profile.from_data(profile_data())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            anchors = Anchors(root, root, root)
            python_route = resolve_route(profile, "python", anchors)
            docs_route = resolve_route(profile, "docs", anchors)
        self.assertEqual(python_route.route.stack, "python")
        self.assertEqual(docs_route.route.stack, "docs")
        self.assertNotEqual(python_route.root, docs_route.root)
        self.assertEqual(docs_route.checks, (("spellcheck",),))

    def test_incomplete_unknown_and_escaping_profiles_fail_before_dispatch(self) -> None:
        incomplete = copy.deepcopy(profile_data())
        del incomplete["registry"]  # type: ignore[index]
        unknown = copy.deepcopy(profile_data())
        unknown["registry"]["task_types"]["unknown"] = unknown["registry"]["task_types"].pop("docs")  # type: ignore[index]
        escaping = copy.deepcopy(profile_data())
        escaping["registry"]["roots"]["app"] = "../outside"  # type: ignore[index]
        with TemporaryDirectory() as directory:
            anchors = Anchors(Path(directory), Path(directory), Path(directory))
            with self.assertRaisesRegex(SchemaError, "registry is required"):
                resolve_route(Profile.from_data(incomplete), "python", anchors)
            with self.assertRaisesRegex(SchemaError, "unknown task type"):
                Profile.from_data(unknown)
            with self.assertRaisesRegex(SchemaError, "must not traverse"):
                Profile.from_data(escaping)

    def test_resolution_uses_only_explicit_anchors_and_rejects_symlink_escape(self) -> None:
        profile = Profile.from_data(profile_data())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project_anchor = root / "project-anchor"
            agents_anchor = root / "agents-anchor"
            core_anchor = root / "core-anchor"
            for anchor in (project_anchor, agents_anchor, core_anchor):
                anchor.mkdir()
            resolved = resolve_paths(profile, Anchors(project_anchor, agents_anchor, core_anchor))
            self.assertEqual(resolved.project, project_anchor / "workspace")
            self.assertEqual(resolved.agents, agents_anchor / "shared")
            self.assertEqual(resolved.core, core_anchor / "core")
            link = project_anchor / "workspace"
            try:
                os.symlink(root, link, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaisesRegex(SchemaError, "escapes its explicit anchor"):
                resolve_route(profile, "python", Anchors(project_anchor, agents_anchor, core_anchor))


class RoleCompositionTests(unittest.TestCase):
    def test_base_extension_and_adapter_grants_cannot_escalate(self) -> None:
        profile = Profile.from_data(profile_data())
        self.assertEqual(compose_role(profile, "executor", ["read", "write"], ["run_checks"]),
                         ("read", "run_checks", "write"))
        self.assertEqual(compose_role(profile, "executor", [], []),
                         ("read", "run_checks", "write"))
        with self.assertRaisesRegex(SchemaError, "escalation"):
            compose_role(profile, "executor", ["read", "network"], ["read"])
        with self.assertRaisesRegex(SchemaError, "escalation"):
            compose_role(profile, "task_verifier", ["read"], ["write"])

    def test_write_capable_verifiers_are_rejected_at_profile_load(self) -> None:
        data = profile_data()
        data["role_grants"]["task_verifier"] = ["read", "write"]  # type: ignore[index]
        with self.assertRaisesRegex(SchemaError, "read-only"):
            Profile.from_data(data)


if __name__ == "__main__":
    unittest.main()
