"""Evidence, redaction, budget, classification, and process-tree tests for run_command."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
import base64
import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline_core.commands import (
    DIAGNOSTIC_OUTPUT_BUDGET,
    ROUTINE_OUTPUT_BUDGET,
    classify_outcome,
    outcome_of,
    run_command,
    run_live_isolation_probe,
    redact_probe_output,
)
from pipeline_core.adapters import AdapterError, CodexAdapter, CompletedProcess, LiveProbeRequest
from pipeline_core.state import EXIT_LAUNCH_FAILED, EXIT_NOT_FOUND, EXIT_TIMEOUT, Run


def _run(root: Path) -> Run:
    prompt = root / "prompt.md"
    prompt.write_text("prompt", encoding="utf-8")
    return Run.create("commands", prompt, None, root / "runs" / "commands", root)


def _emit(text: str, exit_code: int = 0) -> list[str]:
    return [sys.executable, "-c",
            f"import sys; sys.stdout.write({text!r}); sys.exit({exit_code})"]


class OutcomeClassificationTests(unittest.TestCase):
    def test_environmental_failures_are_blocked_and_defects_fail(self) -> None:
        self.assertEqual(classify_outcome(0)[0], "PASS")
        self.assertEqual(classify_outcome(EXIT_NOT_FOUND)[0], "BLOCKED")
        self.assertEqual(classify_outcome(EXIT_LAUNCH_FAILED)[0], "BLOCKED")
        self.assertEqual(classify_outcome(EXIT_TIMEOUT)[0], "FAIL")
        self.assertEqual(classify_outcome(2)[0], "FAIL")
        self.assertIsNotNone(classify_outcome(EXIT_NOT_FOUND)[1])


class BudgetAndRedactionTests(unittest.TestCase):
    def test_budget_is_selected_by_outcome_not_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            oversized = "x" * (ROUTINE_OUTPUT_BUDGET + 5000)

            ok = run_command(run, "verify", ".", _emit(oversized, 0))
            bad = run_command(run, "verify", ".", _emit(oversized, 1))

            self.assertEqual(ok["disposition"], "PASS")
            self.assertLessEqual(len(ok["stdout"].encode("utf-8")), ROUTINE_OUTPUT_BUDGET)
            self.assertIn("truncated", ok["stdout"])

            self.assertEqual(bad["disposition"], "FAIL")
            self.assertEqual(len(bad["stdout"]), len(oversized))
            self.assertLessEqual(len(bad["stdout"].encode("utf-8")), DIAGNOSTIC_OUTPUT_BUDGET)

    def test_secrets_and_paths_are_redacted_before_the_tail_is_taken(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            secret = "AKIAA1B2C3D4E5F6G7H8"
            payload = "A" * (ROUTINE_OUTPUT_BUDGET + 2000) + \
                f"\napi_token={secret}\nworkdir={root}\n"

            record = run_command(run, "verify", ".", _emit(payload, 0))

            self.assertNotIn(secret, record["stdout"])
            self.assertIn("<redacted>", record["stdout"])
            self.assertNotIn(str(root), record["stdout"])
            self.assertIn("truncated", record["stdout"])
            log = Path(run.run_dir) / record["stdout_log"]
            self.assertTrue(log.is_file())
            self.assertNotIn(secret, log.read_text(encoding="utf-8"))

    def test_probe_redaction_removes_the_runner_only_value_before_bounding(self) -> None:
        value = secrets.token_urlsafe(32)
        rendered = redact_probe_output("before " + value + " after", value, 64, ".")
        self.assertNotIn(value, rendered)
        self.assertIn("<redacted-probe>", rendered)

    def test_probe_redaction_removes_common_lossless_marker_encodings(self) -> None:
        value = secrets.token_urlsafe(32)
        encoded = value.encode("utf-8")
        variants = (
            base64.b64encode(encoded).decode("ascii"),
            base64.urlsafe_b64encode(encoded).decode("ascii"),
            encoded.hex(),
        )
        rendered = redact_probe_output(" ".join(variants), value, 4096, ".")
        for variant in variants:
            self.assertNotIn(variant, rendered)
        self.assertEqual(rendered.count("<redacted-probe>"), len(variants))


class EvidenceRecordTests(unittest.TestCase):
    def test_probe_persists_a_redacted_structured_adapter_launch_error_at_its_run_report_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            task = run.add_task("REC-36")
            task.adapter = "codex"
            task.task_contract_digest = "sha256:contract"
            report_path = Path(run.run_dir) / "reports" / "REC-36" / "live-probe.json"
            sentinel = "runner-private-sentinel"
            request = LiveProbeRequest(
                task_id="REC-36", prompt="probe", report_path=report_path,
                allowed_scope=("tests/test_commands.py",), timeout=1.0,
            )
            adapter = SimpleNamespace(
                name="codex",
                launch_live_probe=lambda _: (_ for _ in ()).throw(AdapterError(
                    f"invalid --cd {root} {sentinel}", "probe-invalid-cwd",
                )),
            )

            with patch("pipeline_core.commands.secrets.token_urlsafe", return_value=sentinel):
                evidence = run_live_isolation_probe(
                    run, task_id="REC-36", adapter=adapter, request=request,
                    cli_version="test", task_contract_digest="sha256:contract",
                    bundle_digest="sha256:test", timeout_s=1.0,
                    max_attempts=1, attempt_id="probe-1",
                )

            self.assertEqual(evidence["disposition"], "LAUNCH_FAILED")
            self.assertTrue(report_path.is_file())
            diagnostic = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["type"], "AdapterError")
            self.assertEqual(diagnostic["code"], "probe-invalid-cwd")
            self.assertNotIn(str(root), diagnostic["message"])
            self.assertNotIn(sentinel, json.dumps(diagnostic))

    def test_probe_persists_a_redacted_structured_launch_failure_for_a_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            task = run.add_task("REC-36")
            task.adapter = "codex"
            task.task_contract_digest = "sha256:contract"
            report_path = Path(run.run_dir) / "reports" / "REC-36" / "live-probe.json"
            request = LiveProbeRequest(
                task_id="REC-36", prompt="probe", report_path=report_path,
                allowed_scope=("tests/test_commands.py",), timeout=1.0,
            )
            adapter = SimpleNamespace(
                name="codex",
                launch_live_probe=lambda _: SimpleNamespace(
                    exit_code=1, stdout=f"private path {root}", stderr="failed",
                ),
            )

            evidence = run_live_isolation_probe(
                run, task_id="REC-36", adapter=adapter, request=request,
                cli_version="test", task_contract_digest="sha256:contract",
                bundle_digest="sha256:test", timeout_s=1.0,
                max_attempts=1, attempt_id="probe-1",
            )

            self.assertEqual(evidence["disposition"], "LAUNCH_FAILED")
            self.assertTrue(report_path.is_file())
            diagnostic = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["exit_code"], 1)
            self.assertEqual(diagnostic["disposition"], "LAUNCH_FAILED")
            self.assertEqual(diagnostic["parse_status"], "not-observed")
            self.assertNotIn(str(root), json.dumps(diagnostic))
            self.assertNotIn("stdout", diagnostic)

    def test_probe_failure_persists_each_parse_classification_without_model_output(self) -> None:
        for parse_status in ("no-final-message", "invalid-json", "schema-mismatch"):
            with self.subTest(parse_status=parse_status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = _run(root)
                task = run.add_task("TC-11")
                task.adapter = "codex"
                task.task_contract_digest = "sha256:contract"
                report_path = Path(run.run_dir) / "reports" / "TC-11" / "live-probe.json"
                sentinel = "runner-private-sentinel"
                request = LiveProbeRequest(
                    task_id="TC-11", prompt="private-probe-prompt", report_path=report_path,
                    allowed_scope=("tests/test_commands.py",), timeout=1.0,
                )
                adapter = SimpleNamespace(
                    name="codex",
                    launch_live_probe=lambda _: SimpleNamespace(
                        exit_code=1, stdout="raw model output",
                        stderr="failed", probe_parse_status=parse_status,
                        probe_allowed_write=False, probe_sibling_mounted=False,
                        probe_subprocess_state="not-observed", probe_nested_state="not-observed",
                        probe_stderr_reason="failed private-probe-prompt",
                    ),
                )
                with patch("pipeline_core.commands.secrets.token_urlsafe", return_value=sentinel):
                    run_live_isolation_probe(
                        run, task_id="TC-11", adapter=adapter, request=request,
                        cli_version="test", task_contract_digest="sha256:contract",
                        bundle_digest="sha256:test", timeout_s=1.0,
                        max_attempts=1, attempt_id="probe-11",
                    )

                diagnostic = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(diagnostic["parse_status"], parse_status)
                self.assertIs(diagnostic["allowed_write_observed"], False)
                self.assertIs(diagnostic["sibling_mounted"], False)
                self.assertEqual(diagnostic["subprocess_state"], "not-observed")
                self.assertEqual(diagnostic["nested_state"], "not-observed")
                self.assertEqual(diagnostic["failure_class"], "schema-or-misreport")
                self.assertNotIn("raw model output", json.dumps(diagnostic))
                self.assertNotIn("private-probe-prompt", json.dumps(diagnostic))

    def test_probe_failure_persists_no_observed_write_classification_without_model_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            task = run.add_task("TC-11")
            task.adapter = "codex"
            task.task_contract_digest = "sha256:contract"
            report_path = Path(run.run_dir) / "reports" / "TC-11" / "live-probe.json"
            request = LiveProbeRequest(
                task_id="TC-11", prompt="private-probe-prompt", report_path=report_path,
                allowed_scope=("tests/test_commands.py",), timeout=1.0,
            )
            adapter = SimpleNamespace(
                name="codex",
                launch_live_probe=lambda _: SimpleNamespace(
                    exit_code=1, stdout="raw model output", stderr="failed",
                    probe_parse_status="valid", probe_allowed_write=False,
                    probe_failure_class="no-observed-allowed-write",
                ),
            )

            run_live_isolation_probe(
                run, task_id="TC-11", adapter=adapter, request=request,
                cli_version="test", task_contract_digest="sha256:contract",
                bundle_digest="sha256:test", timeout_s=1.0,
                max_attempts=1, attempt_id="probe-12",
            )

            diagnostic = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["failure_class"], "no-observed-allowed-write")
            self.assertNotIn("raw model output", json.dumps(diagnostic))

    def test_runner_probe_uses_the_probe_entrypoint_when_strict_codex_roles_lack_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            task = run.add_task("REC-36")
            task.adapter = "codex"
            task.task_contract_digest = "sha256:contract"
            request = LiveProbeRequest(
                task_id="REC-36", prompt="probe", report_path=Path("report"),
                allowed_scope=("tests/test_commands.py",), timeout=1.0,
            )
            adapter = CodexAdapter(
                executable="codex",
                runner=lambda *args, **kwargs: CompletedProcess(0, "", ""),
            )

            evidence = run_live_isolation_probe(
                run, task_id="REC-36", adapter=adapter, request=request,
                cli_version="test", task_contract_digest="sha256:contract",
                bundle_digest="sha256:test", timeout_s=1.0,
                max_attempts=1, attempt_id="probe-1",
            )

            self.assertEqual(evidence["disposition"], "NO_BREACH_OBSERVED")
            self.assertEqual(evidence["role"], "runner-live-isolation-probe")

    def test_probe_cleanup_failure_is_persisted_as_a_failed_negative_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            task = run.add_task("REC-36")
            task.adapter = "codex"
            task.task_contract_digest = "sha256:contract"
            request = LiveProbeRequest(
                task_id="REC-36", prompt="probe", report_path=Path("report"),
                allowed_scope=("tests/test_commands.py",), timeout=1.0,
            )
            adapter = SimpleNamespace(
                name="codex", launch_live_probe=lambda _: SimpleNamespace(exit_code=0, stdout="", stderr=""),
            )
            with patch("pipeline_core.commands.shutil.rmtree", side_effect=OSError("locked")):
                evidence = run_live_isolation_probe(
                    run, task_id="REC-36", adapter=adapter, request=request,
                    cli_version="test", task_contract_digest="sha256:contract",
                    bundle_digest="sha256:test", timeout_s=1.0,
                    max_attempts=1, attempt_id="probe-1",
                )
            self.assertEqual(evidence["cleanup"], "failed")
            self.assertEqual(evidence["disposition"], "LAUNCH_FAILED")

    def test_probe_marks_adapter_shape_errors_as_malformed_without_persisting_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            task = run.add_task("REC-36")
            task.adapter = "codex"
            task.task_contract_digest = "sha256:contract"
            request = LiveProbeRequest(
                task_id="REC-36", prompt="probe", report_path=Path("report"),
                allowed_scope=("tests/test_commands.py",), timeout=1.0,
            )
            adapter = SimpleNamespace(name="codex", launch_live_probe=lambda _: SimpleNamespace(exit_code=0))
            evidence = run_live_isolation_probe(
                run, task_id="REC-36", adapter=adapter, request=request,
                cli_version="test", task_contract_digest="sha256:contract",
                bundle_digest="sha256:test", timeout_s=1.0,
                max_attempts=1, attempt_id="probe-1",
            )
            self.assertEqual(evidence["disposition"], "MALFORMED_OUTPUT")

    def test_command_ids_are_stable_and_logs_land_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            first = run_command(run, "verify", ".", _emit("one", 0))
            second = run_command(run, "verify", ".", _emit("two", 0))

            self.assertEqual([first["id"], second["id"]], ["command-1", "command-2"])
            self.assertEqual(run.stage_command_ids("verify"), ["command-1", "command-2"])
            # written before any run.save()
            logs = sorted(p.name for p in (Path(run.run_dir) / "logs").iterdir())
            self.assertEqual(logs, ["verify-1.stdout.txt", "verify-2.stdout.txt"])
            self.assertEqual(outcome_of(second).command_id, "command-2")

    def test_missing_program_and_cwd_are_blocked_with_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            not_found = run_command(run, "verify", ".", ["definitely-not-a-program"])
            self.assertEqual(not_found["exit_code"], EXIT_NOT_FOUND)
            self.assertEqual(not_found["disposition"], "BLOCKED")
            self.assertIn("reason", not_found)

            bad_cwd = run_command(run, "verify", "nope", [sys.executable, "-c", "pass"])
            self.assertEqual(bad_cwd["exit_code"], EXIT_LAUNCH_FAILED)
            self.assertEqual(bad_cwd["disposition"], "BLOCKED")

    def test_empty_argv_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            with self.assertRaises(ValueError):
                run_command(run, "verify", ".", [])


class ProcessTreeTests(unittest.TestCase):
    def test_timeout_leaves_no_child_or_grandchild_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            sentinel = root / "leaked.txt"
            grandchild = root / "grandchild.py"
            child = root / "child.py"
            parent = root / "parent.py"
            grandchild.write_text(
                "import sys, time\n"
                "time.sleep(3)\n"
                "open(sys.argv[1], 'w').write('leaked')\n", encoding="utf-8")
            child.write_text(
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
                "time.sleep(30)\n", encoding="utf-8")
            parent.write_text(
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]])\n"
                "time.sleep(30)\n", encoding="utf-8")

            record = run_command(
                run, "verify", ".",
                [sys.executable, str(parent), str(child), str(grandchild), str(sentinel)],
                timeout=0.5)

            self.assertEqual(record["exit_code"], EXIT_TIMEOUT)
            self.assertEqual(record["disposition"], "FAIL")
            time.sleep(4)
            self.assertFalse(sentinel.exists(), "a descendant process survived the timeout")


if __name__ == "__main__":
    unittest.main()
