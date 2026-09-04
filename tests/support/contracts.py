"""The shared launch-adapter conformance contract (QG-01, AC-2).

Every fake executor/verifier used across the suite — ``ScriptedExecutor``, ``StubVerifier``
(:mod:`tests.support.fakes`), ``FakeVerifier`` (``tests/test_verification.py``), and
``FakeAdapter`` (``tests/test_bootstrap_cli.py``) — accepts one launch request and returns a
:class:`pipeline_core.adapters.LaunchResult`. This module names that structural contract once,
so a new fake is checked against it instead of re-deriving the shape ad hoc per test file.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pipeline_core.adapters import LaunchResult


@runtime_checkable
class LaunchAdapterDouble(Protocol):
    """Structural contract every launch-capable test double satisfies.

    A conforming double takes one positional launch-request object and returns a
    :class:`~pipeline_core.adapters.LaunchResult`. The request's shape is intentionally left
    untyped here: real adapters/verifiers accept ``LaunchRequest``, but fakes are exercised
    with a variety of narrower stand-ins across the suite.
    """

    def launch(self, request: object) -> LaunchResult: ...


class AdapterConformanceMixin:
    """Mixin asserting a fake adapter/verifier honors :class:`LaunchAdapterDouble`.

    Combine with :class:`unittest.TestCase` and implement :meth:`make_double` (return the
    fake under test) and :meth:`make_request` (return a minimal launch request it accepts).
    """

    def make_double(self) -> LaunchAdapterDouble:
        raise NotImplementedError

    def make_request(self) -> object:
        raise NotImplementedError

    def test_double_is_a_launch_adapter(self) -> None:
        double = self.make_double()
        self.assertIsInstance(double, LaunchAdapterDouble)  # type: ignore[attr-defined]

    def test_launch_returns_a_launch_result(self) -> None:
        double = self.make_double()
        result = double.launch(self.make_request())
        self.assertIsInstance(result, LaunchResult)  # type: ignore[attr-defined]
