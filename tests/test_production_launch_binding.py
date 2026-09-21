"""TC-11 production requests retain canonical authority through continuations."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from pipeline_core.adapters import AdapterError, ClaudeAdapter, CodexAdapter, LaunchRequest, LaunchResult
from tests.test_dispatch import ScriptedAdapter, _request, _running_life, _spec, dispatch_executor
from tests.test_verification import (
    FakeVerifier, _implemented_run, _orchestrate, _spec as verifier_spec,
)


class ProductionBindingTests(unittest.TestCase):
    def assert_bound(self, request: LaunchRequest, scope: tuple[str, ...]) -> None:
        self.assertIsNotNone(request.composition)
        self.assertIsNotNone(request.composition.request_digest)
        self.assertEqual(request.allowed_scope, scope)
        for adapter_type in (ClaudeAdapter, CodexAdapter):
            with self.assertRaises(AdapterError) as raised:
                adapter_type(executable='unused').launch(replace(request, prompt='wrong-stack content'))
            self.assertEqual(raised.exception.code, 'role-bundle-substitution')

    def test_executor_initial_and_continuation_are_bound(self) -> None:
        captured: list[LaunchRequest] = []

        class RecordingExecutor(ScriptedAdapter):
            def launch(self, request: LaunchRequest) -> LaunchResult:
                captured.append(request)
                return super().launch(request)

        with tempfile.TemporaryDirectory() as directory:
            spec = _spec()
            outcome = dispatch_executor(
                _running_life(Path(directory), spec), _request(spec), RecordingExecutor(),
            )
            self.assertEqual(outcome.status, 'implemented')
        self.assertEqual(len(captured), 2)
        for request in captured:
            self.assert_bound(request, tuple(spec.allowed_scope))
        self.assertTrue(captured[1].no_tools)
        self.assertTrue(captured[1].read_only)

    def test_independent_verifiers_and_continuations_are_bound(self) -> None:
        captured: list[LaunchRequest] = []

        class RecordingVerifier(FakeVerifier):
            def launch(self, request: LaunchRequest) -> LaunchResult:
                captured.append(request)
                return super().launch(request)

        with tempfile.TemporaryDirectory() as directory:
            spec = verifier_spec()
            outcome = _orchestrate(
                _implemented_run(Path(directory)), spec,
                RecordingVerifier(marker='task-only'), RecordingVerifier(marker='test-only'),
            )
            self.assertIsNone(outcome.failure)
        self.assertEqual(len(captured), 4)
        for request in captured:
            self.assert_bound(request, tuple(spec.allowed_scope))
            self.assertTrue(request.read_only)
            if request.role == 'test_verifier':
                self.assertTrue(request.no_tools)
                self.assertEqual(request.role_grant, ())
                self.assertNotIn('task-only', request.prompt)
