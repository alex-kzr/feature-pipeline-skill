"""Contract tests for portable verification and safety ports."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline_core.adapters import LaunchRequest, LaunchResult
from pipeline_core.archive import ArchiveError, archive_manifest
from pipeline_core.git_port import GitPort, GitSafetyError
from pipeline_core.recovery_descriptors import DescriptorError, load_descriptor
from pipeline_core.verification import VerificationError, VerificationEvidence, parse_verdict, verify_independently


class StubAdapter:
    def launch(self, request: LaunchRequest) -> LaunchResult:
        return LaunchResult(0, '{"verdict": "PASS"}', session_id=request.role)


class VerificationPortTests(unittest.TestCase):
    def test_verdict_requires_an_exact_json_envelope(self) -> None:
        self.assertEqual(parse_verdict('{"verdict": "PASS"}'), "PASS")
        with self.assertRaises(VerificationError):
            parse_verdict("PASS")

    def test_verifiers_must_be_read_only_and_independent(self) -> None:
        evidence = VerificationEvidence("NP-02", 1, ({"exit_code": 0},))
        task = LaunchRequest("task-verifier", "NP-02", "task", Path("task.json"), read_only=True)
        test = LaunchRequest("test-verifier", "NP-02", "test", Path("test.json"), read_only=True)
        self.assertEqual(verify_independently(StubAdapter(), evidence, task, test).status, "verified")
        with self.assertRaises(VerificationError):
            verify_independently(StubAdapter(), evidence, task, LaunchRequest("test", "NP-02", "", Path("x")))


class SafetyPortTests(unittest.TestCase):
    def test_git_denies_push_and_history_rewriting(self) -> None:
        git = GitPort(Path.cwd())
        for argv in (("push",), ("reset", "--hard"), ("status", "--force")):
            with self.subTest(argv=argv):
                with self.assertRaises(GitSafetyError):
                    git.run(argv)

    def test_archive_manifest_refuses_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence.txt").write_text("evidence", encoding="utf-8")
            self.assertEqual(archive_manifest(root, ["evidence.txt"])["total_bytes"], 8)
            with self.assertRaises(ArchiveError):
                archive_manifest(root, ["../outside"])

    def test_descriptor_has_no_global_recovery_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "descriptor.json"
            path.write_text(json.dumps({"id": "repair", "feature": "fixture", "task_id": "T-1", "expected_state": {}, "target_state": {}, "single_use_marker": "once"}), encoding="utf-8")
            self.assertEqual(load_descriptor(path).id, "repair")
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(DescriptorError):
                load_descriptor(path)
