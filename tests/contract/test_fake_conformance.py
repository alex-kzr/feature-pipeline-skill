"""QG-01 — every shared launch-adapter test double honors one structural contract.

``ScriptedExecutor``/``StubVerifier`` (:mod:`tests.support.fakes`), ``FakeVerifier``
(``tests/test_verification.py``), and ``FakeAdapter`` (``tests/test_bootstrap_cli.py``) are
each hand-written per suite. Nothing previously checked that they actually share a launch
contract rather than merely happening to look alike; this suite makes that contract explicit
and reusable (:mod:`tests.support.contracts`) and pins every fake to it (AC-2).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from pipeline_core.adapters import LaunchRequest

from tests.support.contracts import AdapterConformanceMixin
from tests.support.fakes import ScriptedExecutor, StubVerifier
from tests.support.fixtures import temp_root


def _request(report_path: Path) -> LaunchRequest:
    return LaunchRequest(
        role="task_verifier",
        task_id="T-1",
        prompt="prompt text",
        report_path=report_path,
    )


class ScriptedExecutorConformanceTests(AdapterConformanceMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_root()
        self._root = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def make_double(self) -> ScriptedExecutor:
        return ScriptedExecutor(("implemented",))

    def make_request(self) -> LaunchRequest:
        return _request(self._root / "executor-report.md")


class StubVerifierConformanceTests(AdapterConformanceMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_root()
        self._root = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def make_double(self) -> StubVerifier:
        return StubVerifier(("PASS",))

    def make_request(self) -> LaunchRequest:
        return _request(self._root / "verifier-report.md")


class FakeVerifierConformanceTests(AdapterConformanceMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_root()
        self._root = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def make_double(self):
        from tests.test_verification import FakeVerifier

        return FakeVerifier(verdict="PASS")

    def make_request(self) -> LaunchRequest:
        return _request(self._root / "fake-verifier-report.md")


class FakeAdapterConformanceTests(AdapterConformanceMixin, unittest.TestCase):
    def make_double(self):
        from tests.test_bootstrap_cli import FakeAdapter

        return FakeAdapter()

    def make_request(self) -> LaunchRequest:
        return _request(Path("unused-report.md"))


if __name__ == "__main__":
    unittest.main()
