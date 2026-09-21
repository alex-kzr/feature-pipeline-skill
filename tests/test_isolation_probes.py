"""Production strict-isolation probe regression coverage (TC-11)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from feature_pipeline.infrastructure.isolation_probes import (
    DeterministicIsolationVerifier,
    IsolationProbeRecordError,
    ProductionIsolationProbe,
    _assert_runner_owned_location,
    _observed_version,
    load_isolation_capabilities,
)
from feature_pipeline.ports.adapters import STRICT_ISOLATION_CAPABILITIES
from pipeline_core.adapters import AdapterError, ClaudeAdapter, CodexAdapter, LaunchRequest


def _record(
    path: Path, name: str, executable: list[str], surface: str, version: str,
    *, status: str = "PASS", denial: bool = False,
) -> None:
    evidence = "runner probe evidence"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1, "runner_owned": True, "verification_status": status,
        "adapter": name, "executable": "\0".join(executable), "cli_surface": surface,
        "observed_version": version, "evidence": evidence,
        "evidence_digest": hashlib.sha256(evidence.encode()).hexdigest(),
        "capabilities": list(STRICT_ISOLATION_CAPABILITIES),
        **({"fail_closed_adapter_behavior": {
            "status": "PASS", "rejection_code": "stack-isolation-unsupported",
            "child_process_started": False,
        }} if denial else {}),
    }), encoding="utf-8")


class ProductionIsolationProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.executable = [sys.executable]
        self.version = subprocess.run(
            [*self.executable, "--version"], capture_output=True, text=True, check=True,
        ).stdout.strip()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _validator(self, name: str, surface: str) -> ProductionIsolationProbe:
        return ProductionIsolationProbe(
            self.root, name, True, name == "claude", True, True,
        )

    def test_rejects_stale_or_mismatched_observed_version(self) -> None:
        path = self.root / ".pipeline" / "isolation-probes" / "claude.json"
        _record(path, "claude", self.executable, "claude -p", "stale-version")

        with self.assertRaises(IsolationProbeRecordError):
            self._validator("claude", "claude -p").validate(self.executable, "claude -p")

    def test_rejects_record_for_a_different_selected_executable_command(self) -> None:
        path = self.root / ".pipeline" / "isolation-probes" / "codex.json"
        _record(path, "codex", [sys.executable, "-E"], "codex exec", self.version)

        with self.assertRaises(IsolationProbeRecordError):
            self._validator("codex", "codex exec").validate(self.executable, "codex exec")

    def test_rejects_fabricated_record_outside_runner_owned_location(self) -> None:
        _record(self.root / "fabricated.json", "codex", self.executable, "codex exec", self.version)

        with self.assertRaises(IsolationProbeRecordError):
            self._validator("codex", "codex exec").validate(self.executable, "codex exec")

    def test_both_adapters_fail_closed_before_child_launch_for_invalid_probe_evidence(self) -> None:
        for name, adapter_type, surface in (
            ("claude", ClaudeAdapter, "claude -p"),
            ("codex", CodexAdapter, "codex exec"),
        ):
            with self.subTest(adapter=name):
                path = self.root / ".pipeline" / "isolation-probes" / f"{name}.json"
                _record(path, name, self.executable, surface, "stale-version")
                launches: list[object] = []
                adapter = adapter_type(
                    executable=self.executable,
                    runner=lambda *args, **kwargs: launches.append((args, kwargs)),
                    isolation_probe_validator=self._validator(name, surface).validate,
                )
                with self.assertRaises(AdapterError) as raised:
                    adapter.launch(LaunchRequest(
                        role="executor", task_id="TC-11", prompt="x", report_path=Path("report"),
                    ))
                self.assertEqual(raised.exception.code, "stack-isolation-unsupported")
                self.assertEqual(launches, [])

    def test_clean_runner_record_is_diagnostic_only_not_a_verifier_pass(self) -> None:
        path = self.root / ".pipeline" / "isolation-probes" / "claude.json"
        _record(path, "claude", self.executable, "claude -p", self.version, denial=True)

        verdict = DeterministicIsolationVerifier(self.root, "claude").verify(
            task_id="TC-11", role="task_verifier",
        )

        self.assertEqual(verdict.token, "FAIL")
        self.assertEqual(verdict.report["reason"], "probe-not-executor-equivalent")

    def test_passed_diagnostic_record_never_grants_strict_capabilities(self) -> None:
        path = self.root / ".pipeline" / "isolation-probes" / "claude.json"
        _record(path, "claude", self.executable, "claude -p", self.version, denial=True)

        capabilities = load_isolation_capabilities(
            path, name="claude", available=True, supports_resume=True,
            supports_read_only=True, supports_write=True,
        )

        self.assertFalse(any(capabilities.has(token) for token in STRICT_ISOLATION_CAPABILITIES))

    def test_failed_probe_never_becomes_positive_capabilities_or_a_fallback_pass(self) -> None:
        path = self.root / ".pipeline" / "isolation-probes" / "codex.json"
        _record(path, "codex", self.executable, "codex exec", self.version,
                status="FAIL", denial=True)

        verdict = DeterministicIsolationVerifier(self.root, "codex").verify(
            task_id="TC-11", role="test_verifier",
        )

        with self.assertRaises(IsolationProbeRecordError):
            load_isolation_capabilities(
                path, name="codex", available=True, supports_resume=False,
                supports_read_only=True, supports_write=True,
            )
        self.assertEqual(verdict.token, "FAIL")

    def test_missing_and_malformed_records_fail_closed(self) -> None:
        path = self.root / ".pipeline" / "isolation-probes" / "codex.json"
        baseline = load_isolation_capabilities(
            path, name="codex", available=True, supports_resume=False,
            supports_read_only=True, supports_write=True,
        )
        self.assertFalse(any(baseline.has(token) for token in STRICT_ISOLATION_CAPABILITIES))

        path.parent.mkdir(parents=True)
        for payload, message in (("{", "unreadable"), ("[]", "must be an object"),
                                 (json.dumps({"schema_version": 1}), "incomplete")):
            with self.subTest(payload=payload):
                path.write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(IsolationProbeRecordError, message):
                    load_isolation_capabilities(
                        path, name="codex", available=True, supports_resume=False,
                        supports_read_only=True, supports_write=True,
                    )

    def test_version_observation_validates_executable_and_non_tc11_verdict(self) -> None:
        self.assertEqual(_observed_version(self.executable), self.version)
        with self.assertRaisesRegex(IsolationProbeRecordError, "executable is unavailable"):
            _observed_version([])

        verdict = DeterministicIsolationVerifier(self.root, "codex").verify(
            task_id="TC-10", role="task_verifier",
        )
        self.assertEqual(verdict.token, "FAIL")
        self.assertEqual(verdict.report["reason"], "deterministic-isolation-verifier-not-applicable")

    def test_runner_owned_record_and_version_probe_fail_closed_on_execution_errors(self) -> None:
        approved = self.root / ".pipeline" / "isolation-probes" / "codex.json"
        with self.assertRaisesRegex(IsolationProbeRecordError, "record is unavailable"):
            _assert_runner_owned_location(approved, project_dir=self.root, name="codex")

        foreign = self.root / "foreign.json"
        foreign.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(IsolationProbeRecordError, "not runner-owned"):
            _assert_runner_owned_location(foreign, project_dir=self.root, name="codex")

        with patch(
            "feature_pipeline.infrastructure.isolation_probes.subprocess.run",
            side_effect=OSError("unavailable"),
        ):
            with self.assertRaisesRegex(IsolationProbeRecordError, "version is unavailable"):
                _observed_version(self.executable)
        with patch(
            "feature_pipeline.infrastructure.isolation_probes.subprocess.run",
            return_value=subprocess.CompletedProcess(self.executable, 1, stdout="", stderr="bad"),
        ):
            with self.assertRaisesRegex(IsolationProbeRecordError, "version is unavailable"):
                _observed_version(self.executable)


if __name__ == "__main__":
    unittest.main()
