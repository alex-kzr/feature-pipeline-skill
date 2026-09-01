"""Ordering, required-section, and redaction tests for the pre-transition diagnostic."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from pipeline_core.commands import run_command
from pipeline_core.diagnostics import SECTION_ORDER, write_diagnostic_report
from pipeline_core.state import Run


def _run(root: Path) -> Run:
    prompt = root / "prompt.md"
    prompt.write_text("prompt", encoding="utf-8")
    run = Run.create("diagnostics", prompt, None, root / "runs" / "diagnostics", root)
    run.add_task("T-1")
    for status in ("ready", "running"):
        run.transition_task("T-1", status)
    return run


class DiagnosticReportTests(unittest.TestCase):
    def test_required_sections_appear_in_the_contract_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run_command(run, "task:T-1", ".",
                        [sys.executable, "-c", "print('hello from the tool')"])

            path = write_diagnostic_report(
                run, task_id="T-1", attempt=1,
                git_diff="diff --git a b", git_status="M pipeline_core/x.py")

            self.assertEqual(path.name, "diagnostic-report.md")
            text = path.read_text(encoding="utf-8")
            positions = [text.index(f"## {title}") for title in SECTION_ORDER]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("hello from the tool", text)
            self.assertIn("M pipeline_core/x.py", text)

    def test_recent_commands_are_capped_and_current_output_is_the_last_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            for index in range(7):
                run_command(run, "task:T-1", ".",
                            [sys.executable, "-c", f"print('run {index}')"])

            text = write_diagnostic_report(run, task_id="T-1", attempt=2).read_text(encoding="utf-8")
            recent_block = text.split("## Recent commands", 1)[1].split("## ", 1)[0]
            self.assertEqual(recent_block.count("-> exit"), 5)
            self.assertIn("run 6", text)
            self.assertNotIn("run 1", recent_block)

    def test_secrets_and_local_paths_are_redacted_in_output_and_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            run_command(run, "task:T-1", ".",
                        [sys.executable, "-c", f"print('token={secret}')"])

            text = write_diagnostic_report(
                run, task_id="T-1", attempt=1,
                git_diff=f"+ home = {root}\n+ key = {secret}",
                git_status="clean").read_text(encoding="utf-8")

            self.assertNotIn(secret, text)
            self.assertNotIn(str(root), text)
            self.assertIn("<redacted>", text)

    def test_report_is_standalone_and_referenced_without_saving_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            path = write_diagnostic_report(run, task_id="T-1", attempt=1, note="verifier said FAIL")

            self.assertTrue(path.is_file())
            self.assertIn("verifier said FAIL", path.read_text(encoding="utf-8"))
            self.assertEqual(
                run.artifacts["diagnostic:T-1:1"],
                path.resolve().relative_to(Path(run.run_dir).resolve()).as_posix())
            self.assertFalse((Path(run.run_dir) / "run.json").exists())


if __name__ == "__main__":
    unittest.main()
