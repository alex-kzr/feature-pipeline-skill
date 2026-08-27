"""Contract tests for portable profile inputs and unsafe input denials."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from schemas import (
    Profile,
    RunState,
    SchemaError,
    TaskMetadata,
    ToolStage,
    Verdict,
    ensure_no_role_escalation,
    load_profile,
    validate_relative_path,
)


ROOT = Path(__file__).resolve().parents[1]


def fixture_data(path: str) -> dict[str, object]:
    return json.loads((ROOT / "fixtures" / path).read_text(encoding="utf-8"))


class ProfileFixtureTests(unittest.TestCase):
    def test_two_independent_profiles_load_with_distinct_mount_forms(self) -> None:
        first = load_profile(ROOT / "fixtures/library-guide/profile.json")
        second = load_profile(ROOT / "fixtures/incident-notes/config/pipeline.json")

        self.assertEqual(first.logical_paths.agents, "shared/skills")
        self.assertEqual(second.logical_paths.agents, ".agents/skills")
        self.assertNotEqual(first.name, second.name)

    def test_unsafe_profile_fails_before_state_artifact_exists(self) -> None:
        profile = fixture_data("library-guide/profile.json")
        profile["logical_paths"]["core"] = "../outside"  # type: ignore[index]
        with TemporaryDirectory() as directory:
            target = Path(directory) / "profile.json"
            target.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaises(SchemaError):
                load_profile(target)
            self.assertEqual(list(Path(directory).glob("run-state*")), [])


class SchemaDenialTests(unittest.TestCase):
    def test_rejects_absolute_and_traversal_logical_paths(self) -> None:
        for value in ("C:/private", "/private", "~/private", "safe/../private"):
            with self.subTest(value=value):
                with self.assertRaises(SchemaError):
                    validate_relative_path(value, "logical path")

    def test_rejects_unknown_versions_types_and_subagents(self) -> None:
        with self.assertRaises(SchemaError):
            Profile.from_data({"version": 2})
        with self.assertRaises(SchemaError):
            TaskMetadata.from_data({
                "version": 1, "type": "unknown", "executor": "executor",
                "verifiers": ["task_verifier", "test_verifier"],
            })
        with self.assertRaises(SchemaError):
            ToolStage.from_data({"name": "run", "subagents": ["observer"], "argv": ["run"]})

    def test_rejects_argv_shell_tokens_and_missing_verifier_pair(self) -> None:
        with self.assertRaises(SchemaError):
            ToolStage.from_data({"name": "run", "subagents": ["executor"], "argv": ["run", "&&", "delete"]})
        with self.assertRaises(SchemaError):
            ToolStage.from_data({"name": "run", "subagents": ["executor"], "argv": ["run; delete"]})
        with self.assertRaises(SchemaError):
            TaskMetadata.from_data({
                "version": 1, "type": "python", "executor": "executor", "verifiers": ["task_verifier"],
            })

    def test_rejects_role_escalation(self) -> None:
        with self.assertRaises(SchemaError):
            ensure_no_role_escalation(["read"], ["read", "write"])


class SchemaAcceptanceTests(unittest.TestCase):
    def test_versioned_task_verdict_and_run_state_contracts(self) -> None:
        task = TaskMetadata.from_data({
            "version": 1, "type": "python", "executor": "executor",
            "verifiers": ["task_verifier", "test_verifier"],
        })
        verdict = Verdict.from_data({
            "version": 1, "task_id": "CF-02", "task_verifier": "PASS", "test_verifier": "PASS",
        })
        state = RunState.from_data({"version": 1, "run_id": "run-02", "status": "planned"})

        self.assertEqual(task.version, verdict.version)
        self.assertEqual(state.status, "planned")


if __name__ == "__main__":
    unittest.main()
