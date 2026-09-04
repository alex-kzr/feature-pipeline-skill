"""RS-01 — one local process implementation behind adapters and verification commands.

Covers the new :mod:`feature_pipeline.ports.process` port and its
:class:`feature_pipeline.infrastructure.process.runner.LocalProcessRunner`, the
:class:`feature_pipeline.infrastructure.adapters.claude_launcher.ClaudeLauncher` bridge, the
shared byte-aware tail, and the two migrated ``pipeline_core`` call sites.
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from feature_pipeline.infrastructure.adapters.claude_launcher import ClaudeLauncher
from feature_pipeline.infrastructure.process.capture import TRUNCATION_MARKER, tail_truncate
from feature_pipeline.infrastructure.process.runner import (
    LocalProcessRunner,
    terminate_process_tree,
)
from feature_pipeline.ports.process import (
    EXIT_TIMEOUT,
    ProcessError,
    ProcessOutcome,
    ProcessRunner,
    ProcessSpec,
)


def _py(script: str) -> list[str]:
    return [sys.executable, "-c", script]


class PortContractTests(unittest.TestCase):
    def test_local_runner_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(LocalProcessRunner(), ProcessRunner)

    def test_spec_normalises_argv_to_a_tuple_of_str(self) -> None:
        spec = ProcessSpec(argv=["a", Path("b")])  # type: ignore[list-item]
        self.assertEqual(spec.argv, ("a", "b"))

    def test_empty_argv_is_refused_with_a_stable_code(self) -> None:
        with self.assertRaises(ProcessError) as ctx:
            LocalProcessRunner().run(ProcessSpec(argv=()))
        self.assertEqual(ctx.exception.code, "empty-argv")

    def test_outcome_ok_predicate(self) -> None:
        self.assertTrue(ProcessOutcome(exit_code=0).ok)
        self.assertFalse(ProcessOutcome(exit_code=1).ok)
        self.assertFalse(ProcessOutcome(exit_code=None, timed_out=True).ok)


class LocalRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = LocalProcessRunner()

    def test_captures_exit_code_and_both_streams(self) -> None:
        out = self.runner.run(
            ProcessSpec(
                argv=_py("import sys; sys.stdout.write('O'); sys.stderr.write('E'); sys.exit(3)")
            )
        )
        self.assertEqual(out.exit_code, 3)
        self.assertEqual(out.stdout, "O")
        self.assertEqual(out.stderr, "E")
        self.assertFalse(out.timed_out)
        self.assertGreaterEqual(out.duration_s, 0.0)

    def test_feeds_stdin_then_closes_it(self) -> None:
        out = self.runner.run(
            ProcessSpec(argv=_py("import sys; sys.stdout.write(sys.stdin.read())"), stdin="hello")
        )
        self.assertEqual(out.stdout, "hello")

    def test_stdin_is_closed_even_when_not_supplied(self) -> None:
        out = self.runner.run(
            ProcessSpec(
                argv=_py("import sys; sys.stdout.write(repr(sys.stdin.read()))"),
                timeout=15,
            )
        )
        self.assertFalse(out.timed_out)
        self.assertEqual(out.stdout, "''")

    def test_utf8_is_pinned_regardless_of_host_locale(self) -> None:
        payload = "héllo—✓—Ω"
        out = self.runner.run(
            ProcessSpec(argv=_py("import sys; sys.stdout.write(sys.stdin.read())"), stdin=payload)
        )
        self.assertEqual(out.stdout, payload)

    def test_env_none_inherits_and_a_mapping_replaces(self) -> None:
        read_var = _py("import os, sys; sys.stdout.write(os.environ.get('RS01_MARK', 'unset'))")
        os.environ["RS01_MARK"] = "inherited"
        try:
            self.assertEqual(self.runner.run(ProcessSpec(argv=read_var)).stdout, "inherited")
            replaced = self.runner.run(
                ProcessSpec(argv=read_var, env={**os.environ, "RS01_MARK": "explicit"})
            )
            self.assertEqual(replaced.stdout, "explicit")
        finally:
            del os.environ["RS01_MARK"]

    def test_missing_executable_raises_start_failed(self) -> None:
        with self.assertRaises(ProcessError) as ctx:
            self.runner.run(ProcessSpec(argv=["rs01-definitely-not-a-real-program"]))
        self.assertEqual(ctx.exception.code, "start-failed")

    def test_timeout_flags_timed_out_and_keeps_partial_output(self) -> None:
        out = self.runner.run(
            ProcessSpec(
                argv=_py(
                    "import sys, time; sys.stdout.write('early'); sys.stdout.flush(); time.sleep(30)"
                ),
                timeout=0.5,
            )
        )
        self.assertTrue(out.timed_out)
        self.assertIsNone(out.exit_code)
        self.assertIn("early", out.stdout)

    def test_timeout_terminates_the_whole_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "descendant.touched"
            child = root / "child.py"
            parent = root / "parent.py"
            child.write_text(
                "import time\n"
                "time.sleep(1.0)\n"
                f"open({str(sentinel)!r}, 'w').close()\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            parent.write_text(
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            out = self.runner.run(
                ProcessSpec(argv=[sys.executable, str(parent)], timeout=0.5)
            )
            self.assertTrue(out.timed_out)
            time.sleep(2.5)
            self.assertFalse(sentinel.exists(), "a descendant survived the timeout")

    def test_terminate_process_tree_reaps_a_live_child(self) -> None:
        import subprocess

        proc = subprocess.Popen(
            _py("import time; time.sleep(30)"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        terminate_process_tree(proc)
        self.assertIsNotNone(proc.poll())

    def test_terminate_process_tree_is_a_noop_on_an_exited_process(self) -> None:
        import subprocess

        proc = subprocess.Popen(
            _py("pass"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.communicate()
        terminate_process_tree(proc)  # already exited: must not raise
        self.assertEqual(proc.poll(), 0)


class CaptureTests(unittest.TestCase):
    def test_short_text_passes_through(self) -> None:
        self.assertEqual(tail_truncate("abc", 4096), "abc")
        self.assertEqual(tail_truncate("", 10), "")

    def test_keeps_the_tail_with_an_explicit_header_inside_budget(self) -> None:
        cut = tail_truncate("z" * 5000, 256)
        self.assertLessEqual(len(cut.encode("utf-8")), 256)
        self.assertIn("bytes truncated; showing the last", cut)

    def test_degrades_to_the_marker_when_even_the_header_will_not_fit(self) -> None:
        self.assertEqual(tail_truncate("z" * 100, 3), TRUNCATION_MARKER[:3])

    def test_parity_with_the_former_commands_helper(self) -> None:
        from pipeline_core import commands

        for text, budget in [("", 10), ("abc", 10), ("λ" * 60, 33), ("z" * 5000, 200)]:
            self.assertEqual(tail_truncate(text, budget), commands._tail_truncate(text, budget))

    def test_as_text_normalises_bytes_str_and_none(self) -> None:
        from feature_pipeline.infrastructure.process.runner import _as_text

        self.assertEqual(_as_text(None), "")
        self.assertEqual(_as_text(b"by\xe2\x9c\x93tes"), "by✓tes")
        self.assertEqual(_as_text("plain"), "plain")


class ClaudeLauncherTests(unittest.TestCase):
    def test_builds_the_spec_from_the_launch_inputs(self) -> None:
        seen: dict[str, ProcessSpec] = {}

        class FakeRunner:
            def run(self, spec: ProcessSpec) -> ProcessOutcome:
                seen["spec"] = spec
                return ProcessOutcome(exit_code=0, stdout="ok", stderr="", duration_s=0.01)

        out = ClaudeLauncher(runner=FakeRunner()).run(
            ["claude", "-p"], prompt="P", cwd="/w", timeout=12, env={"A": "b"}
        )
        self.assertEqual(out.stdout, "ok")
        spec = seen["spec"]
        self.assertEqual(spec.argv, ("claude", "-p"))
        self.assertEqual(spec.stdin, "P")
        self.assertEqual(spec.cwd, "/w")
        self.assertEqual(spec.timeout, 12)
        self.assertEqual(dict(spec.env or {}), {"A": "b"})

    def test_timeout_appends_the_adapter_note_to_stderr(self) -> None:
        class TimingOut:
            def run(self, spec: ProcessSpec) -> ProcessOutcome:
                return ProcessOutcome(
                    exit_code=None, stdout="partial", stderr="boom", duration_s=0.5, timed_out=True
                )

        out = ClaudeLauncher(runner=TimingOut()).run(["claude"], timeout=7)
        self.assertTrue(out.timed_out)
        self.assertIn("boom", out.stderr)
        self.assertIn("timed out after 7s", out.stderr)

    def test_start_failure_propagates_as_process_error(self) -> None:
        class Failing:
            def run(self, spec: ProcessSpec) -> ProcessOutcome:
                raise ProcessError("nope", "start-failed")

        with self.assertRaises(ProcessError):
            ClaudeLauncher(runner=Failing()).run(["claude"])


class MigratedCallSiteTests(unittest.TestCase):
    """AC-1 — adapters and commands both spawn through the one implementation."""

    def test_run_subprocess_is_built_on_the_claude_launcher(self) -> None:
        import pipeline_core.adapters as adapters_mod

        src = inspect.getsource(adapters_mod.run_subprocess)
        self.assertIn("ClaudeLauncher", src)
        self.assertNotIn("subprocess.Popen", src)

    def test_run_command_spawns_through_the_local_process_runner(self) -> None:
        import pipeline_core.commands as commands_mod

        src = inspect.getsource(commands_mod)
        self.assertIn("LocalProcessRunner", src)
        run_src = inspect.getsource(commands_mod.run_command)
        self.assertNotIn("subprocess.Popen", run_src)

    def test_run_subprocess_still_returns_a_completed_process_with_the_timeout_sentinel(self) -> None:
        from pipeline_core.adapters import EXIT_TIMEOUT as ADAPTER_EXIT_TIMEOUT
        from pipeline_core.adapters import run_subprocess

        completed = run_subprocess(
            _py("import time; time.sleep(30)"), prompt="", timeout=0.5
        )
        self.assertEqual(completed.exit_code, ADAPTER_EXIT_TIMEOUT)
        self.assertEqual(ADAPTER_EXIT_TIMEOUT, EXIT_TIMEOUT)

    def test_run_subprocess_maps_a_start_failure_to_adapter_unavailable(self) -> None:
        from pipeline_core.adapters import AdapterError, run_subprocess

        with self.assertRaises(AdapterError) as ctx:
            run_subprocess(["rs01-definitely-not-a-real-program"], prompt="")
        self.assertEqual(ctx.exception.code, "adapter-unavailable")

    def test_run_command_timeout_still_leaves_no_survivor_and_records_timeout(self) -> None:
        from pipeline_core.commands import run_command
        from pipeline_core.state import EXIT_TIMEOUT as STATE_EXIT_TIMEOUT
        from pipeline_core.state import Run

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompt.md").write_text("p", encoding="utf-8")
            run = Run.create("rs01", root / "prompt.md", None, root / "runs" / "rs01", root)
            record = run_command(
                run, "verify", ".", _py("import time; time.sleep(30)"), timeout=0.5
            )
        self.assertEqual(record["exit_code"], STATE_EXIT_TIMEOUT)
        self.assertEqual(record["disposition"], "FAIL")


if __name__ == "__main__":
    unittest.main()
