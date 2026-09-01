"""Evidence, redaction, budget, classification, and process-tree tests for run_command."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from pipeline_core.commands import (
    DIAGNOSTIC_OUTPUT_BUDGET,
    ROUTINE_OUTPUT_BUDGET,
    classify_outcome,
    outcome_of,
    run_command,
)
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


class EvidenceRecordTests(unittest.TestCase):
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
