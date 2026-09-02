"""RDS-13 regression: the executor and task-verifier launches must carry a concrete ``--tools``.

Every existing test that reaches ``build_claude_argv`` hands it ``tools=`` by hand, and every
test that drives the real ``execute`` orchestration uses a fake adapter that never inspects the
argv — so nothing caught that ``dispatch_executor`` and ``orchestrate_verification`` both built
``LaunchRequest.tools == ()`` and the actual CLI invocation carried ``--tools ""``. A tool-less
executor cannot ``Read``/``Bash``/``Edit`` anything; it hallucinates file content or hangs.

These tests close exactly that seam: they run the *real* ``run_task`` path with adapters that
record every :class:`LaunchRequest`, rebuild the argv the production adapter would, and assert
the concrete ``--tools`` set the role's prompt promises actually reaches the CLI.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline_core.adapters import WRITING_TOOLS, build_claude_argv
from pipeline_core.execution import run_task

from tests.test_execution import _execution, _run, _spec
from tests.test_repair import ScriptedExecutor, StubVerifier


def _tools_arg(request) -> list[str]:  # noqa: ANN001 - test helper
    """The ``--tools`` value the production adapter would put on the wire for ``request``."""
    argv = build_claude_argv(request, executable="claude")
    value = argv[argv.index("--tools") + 1]
    return value.split(",") if value else []


class _RecordingExecutor(ScriptedExecutor):
    def __init__(self) -> None:
        super().__init__(("implemented",))
        self.requests: list[object] = []

    def launch(self, request):  # noqa: ANN001 - test double
        self.requests.append(request)
        return super().launch(request)

    def main_request(self):
        """The initial implementation launch — not the status-envelope continuation."""
        return next(r for r in self.requests if not r.resume_session_id)


class _RecordingVerifier(StubVerifier):
    def __init__(self, verdicts: tuple[str, ...]) -> None:
        super().__init__(verdicts)
        self.requests: list[object] = []

    def launch(self, request):  # noqa: ANN001 - test double
        self.requests.append(request)
        return super().launch(request)

    def main_request(self):
        """The initial verdict launch — not the status-envelope continuation."""
        return next(r for r in self.requests if not r.resume_session_id)


class ConcreteToolGrantRegressionTests(unittest.TestCase):
    def _drive(self, root: Path):
        life = _run(root)
        spec = _spec()
        executor = _RecordingExecutor()
        task = _RecordingVerifier(("PASS",))
        test = _RecordingVerifier(("PASS",))
        result = run_task(life, _execution(spec, executor, task, test))
        self.assertEqual(result.status, "verified", result.blocker)
        return executor, task, test

    def test_executor_launch_carries_read_run_and_write_tools(self) -> None:  # AC-1
        with tempfile.TemporaryDirectory() as directory:
            executor, _task, _test = self._drive(Path(directory))
            request = executor.main_request()
            self.assertNotEqual(request.tools, ())
            tools = _tools_arg(request)
            for expected in ("Read", "Glob", "Grep", "Bash", "Edit", "Write"):
                self.assertIn(expected, tools)

    def test_task_verifier_launch_is_read_only_but_not_tool_less(self) -> None:  # AC-2
        with tempfile.TemporaryDirectory() as directory:
            _executor, task, _test = self._drive(Path(directory))
            request = task.main_request()
            self.assertFalse(request.no_tools)
            self.assertNotEqual(request.tools, ())
            tools = _tools_arg(request)
            for expected in ("Read", "Glob", "Grep"):
                self.assertIn(expected, tools)
            for writer in WRITING_TOOLS:
                self.assertNotIn(writer, tools)

    def test_test_verifier_launch_stays_deliberately_tool_free(self) -> None:  # AC-3
        with tempfile.TemporaryDirectory() as directory:
            _executor, _task, test = self._drive(Path(directory))
            request = test.main_request()
            self.assertTrue(request.no_tools)
            self.assertEqual(_tools_arg(request), [])


if __name__ == "__main__":
    unittest.main()
