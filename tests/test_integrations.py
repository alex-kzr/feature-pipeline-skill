"""Tests for the project tool-integration contract loader (integrations.json)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline_core.integrations import (
    GraphifyIntegration,
    is_forbidden_installer_argv,
    is_forbidden_installer_command,
    load_graphify_integration,
)
from schemas import SchemaError

_VALID = {
    "schema_version": 1,
    "graphify": {
        "stage": "graphify",
        "executor": "runner:graphify",
        "workspace": "tools/graphify",
        "scan_root": ".",
        "wrapper_dir": "tools/graphify/scripts",
        "expected_outputs": [
            "tools/graphify/graphify-out/graph.json",
            "tools/graphify/graphify-out/manifest.json",
        ],
        "diff_policy": "tracked-empty",
        "forbidden": [
            "graphify claude install",
            "graphify codex install",
            "graphify antigravity install",
        ],
    },
}


def _write(payload: dict) -> Path:
    directory = Path(tempfile.mkdtemp())
    path = directory / "integrations.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class InstallerDetectionTests(unittest.TestCase):
    def test_detects_every_graphify_install_shape(self) -> None:
        for argv in (
            ["graphify", "install"],
            ["graphify", "claude", "install"],
            ["/opt/bin/graphify", "codex", "install", "--force"],
            ["graphify.exe", "antigravity", "install"],
        ):
            self.assertTrue(is_forbidden_installer_argv(argv), argv)

    def test_leaves_non_installer_graphify_and_wrappers_alone(self) -> None:
        for argv in (
            ["graphify", "update", "."],
            ["bash", "tools/graphify/scripts/build.sh"],
            ["pwsh", "-File", "tools/graphify/scripts/build.ps1"],
        ):
            self.assertFalse(is_forbidden_installer_argv(argv), argv)

    def test_command_string_form(self) -> None:
        self.assertTrue(is_forbidden_installer_command("graphify claude install"))
        self.assertFalse(is_forbidden_installer_command("graphify update ."))


class LoaderTests(unittest.TestCase):
    def test_loads_a_valid_contract(self) -> None:
        integration = load_graphify_integration(_write(_VALID))
        self.assertIsInstance(integration, GraphifyIntegration)
        self.assertEqual(integration.stage, "graphify")
        self.assertEqual(integration.diff_policy, "tracked-empty")
        self.assertEqual(len(integration.expected_outputs), 2)
        self.assertTrue(integration.forbids(["graphify", "claude", "install"]))
        self.assertFalse(integration.forbids(["bash", "tools/graphify/scripts/build.sh"]))

    def test_unknown_schema_version_is_refused(self) -> None:
        payload = json.loads(json.dumps(_VALID))
        payload["schema_version"] = 2
        with self.assertRaisesRegex(SchemaError, "schema_version"):
            load_graphify_integration(_write(payload))

    def test_missing_graphify_object_is_refused(self) -> None:
        with self.assertRaises(SchemaError):
            load_graphify_integration(_write({"schema_version": 1}))

    def test_empty_expected_outputs_is_refused(self) -> None:
        payload = json.loads(json.dumps(_VALID))
        payload["graphify"]["expected_outputs"] = []
        with self.assertRaisesRegex(SchemaError, "expected_outputs"):
            load_graphify_integration(_write(payload))

    def test_unknown_diff_policy_is_refused(self) -> None:
        payload = json.loads(json.dumps(_VALID))
        payload["graphify"]["diff_policy"] = "whatever"
        with self.assertRaisesRegex(SchemaError, "diff_policy"):
            load_graphify_integration(_write(payload))

    def test_absolute_output_path_is_refused(self) -> None:
        payload = json.loads(json.dumps(_VALID))
        payload["graphify"]["expected_outputs"] = ["/etc/passwd"]
        with self.assertRaises(SchemaError):
            load_graphify_integration(_write(payload))

    def test_a_non_installer_forbidden_entry_is_refused(self) -> None:
        payload = json.loads(json.dumps(_VALID))
        payload["graphify"]["forbidden"] = ["graphify update ."]
        with self.assertRaisesRegex(SchemaError, "install"):
            load_graphify_integration(_write(payload))


if __name__ == "__main__":
    unittest.main()
