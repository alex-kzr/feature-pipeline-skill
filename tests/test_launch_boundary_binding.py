"""TC-11 adversarial checks at the public process-launch boundary."""
from dataclasses import replace
from pathlib import Path
import pipeline_core.adapters as adapters
import tempfile
import unittest
from unittest.mock import Mock

from pipeline_core.adapters import (
    AdapterError, ClaudeAdapter, CodexAdapter, DockerCodexAdapter, LaunchRequest,
    LaunchComposition,
)


class DockerProbeBoundaryTests(unittest.TestCase):
    def test_safe_scope_patterns_preserve_their_relative_grants(self) -> None:
        request = LaunchRequest(
            role='python-executor', task_id='TC-11', prompt='content',
            report_path=Path('report.md'),
            allowed_scope=('feature-pipeline-skill/src/**', 'tests/test_*.py',
                           'src\\package\\*.py'),
        )
        self.assertEqual(
            adapters._scoped_container_patterns(request, Path('/repo/feature-pipeline-skill')),
            ('src/**', 'tests/test_*.py', 'src/package/*.py'),
        )

    def test_unsafe_scope_is_rejected_before_any_container_process(self) -> None:
        from feature_pipeline.ports.adapters import IsolationCapabilityProof

        image = 'image@sha256:' + 'a' * 64
        capabilities = replace(
            adapters.CODEX_ISOLATION_CAPABILITIES,
            **{'supports_' + token: True for token in adapters.STRICT_ISOLATION_CAPABILITIES},
            runtime=image, cli_surface='codex exec', observed_version='codex 1.2.3',
            isolation_proofs=tuple(IsolationCapabilityProof(
                token, 'codex', image, 'codex exec', 'codex 1.2.3', 'fixture-only proof',
            ) for token in adapters.STRICT_ISOLATION_CAPABILITIES),
        )
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / 'auth.json'
            auth.write_text('{}', encoding='utf-8')
            for scope in ('/src/**', '../src/**', 'src/../other/**', 'C:/src/**',
                          'src/../../**', '', 'src/\x00*'):
                with self.subTest(scope=scope):
                    inspect, runner, control = Mock(return_value=False), Mock(), Mock()
                    adapter = DockerCodexAdapter(
                        image=image, proxy_image='proxy@sha256:' + 'b' * 64,
                        codex_version='1.2.3', auth_file=auth,
                        isolation_capabilities=capabilities, image_validator=inspect,
                        runner=runner, docker_runner=control,
                    )
                    request = adapters.bind_launch_request(LaunchRequest(
                        role='python-executor', task_id='TC-11', prompt='permitted content',
                        report_path=Path('report.md'), working_root=directory,
                        role_grant=('read', 'write'), allowed_scope=(scope,),
                        recipient_role='executor', bundle_digest='a' * 64,
                        composition=LaunchComposition(
                            'executor', 'a' * 64, (scope,), ('read', 'write'),
                        ),
                    ))
                    with self.assertRaises(AdapterError) as raised:
                        adapter.launch(request)
                    self.assertEqual(raised.exception.code, 'stack-isolation-unsupported')
                    inspect.assert_not_called()
                    runner.assert_not_called()
                    control.assert_not_called()

    def test_container_rejects_proof_for_a_different_pinned_version(self) -> None:
        from feature_pipeline.ports.adapters import IsolationCapabilityProof

        image = 'image@sha256:' + 'a' * 64
        capabilities = replace(
            adapters.CODEX_ISOLATION_CAPABILITIES,
            **{'supports_' + token: True for token in adapters.STRICT_ISOLATION_CAPABILITIES},
            runtime=image, cli_surface='codex exec', observed_version='codex 1.2.2',
            isolation_proofs=tuple(IsolationCapabilityProof(
                token, 'codex', image, 'codex exec', 'codex 1.2.2', 'fixture-only proof',
            ) for token in adapters.STRICT_ISOLATION_CAPABILITIES),
        )
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / 'auth.json'
            auth.write_text('{}', encoding='utf-8')
            inspect = Mock(return_value=False)
            runner = Mock()
            control = Mock()
            adapter = DockerCodexAdapter(
                image=image, proxy_image='proxy@sha256:' + 'b' * 64,
                codex_version='1.2.3', auth_file=auth,
                isolation_capabilities=capabilities, image_validator=inspect,
                runner=runner, docker_runner=control,
            )
            request = adapters.bind_launch_request(LaunchRequest(
                role='python-executor', task_id='TC-11', prompt='permitted content',
                report_path=Path('report.md'), role_grant=('read', 'write'),
                allowed_scope=('src/**',), recipient_role='executor',
                bundle_digest='a' * 64,
                composition=LaunchComposition(
                    'executor', 'a' * 64, ('src/**',), ('read', 'write'),
                ),
            ))
            with self.assertRaises(AdapterError) as raised:
                adapter.launch(request)
            self.assertEqual(raised.exception.code, 'stack-isolation-unsupported')
            inspect.assert_not_called()
            runner.assert_not_called()
            control.assert_not_called()

    def test_write_container_rejects_read_only_requests_before_inspection(self) -> None:
        from feature_pipeline.ports.adapters import IsolationCapabilityProof

        image = 'image@sha256:' + 'a' * 64
        capabilities = replace(
            adapters.CODEX_ISOLATION_CAPABILITIES,
            **{'supports_' + token: True for token in adapters.STRICT_ISOLATION_CAPABILITIES},
            runtime=image, cli_surface='codex exec', observed_version='codex 1.2.3',
            isolation_proofs=tuple(IsolationCapabilityProof(
                token, 'codex', image, 'codex exec', 'codex 1.2.3', 'fixture-only proof',
            ) for token in adapters.STRICT_ISOLATION_CAPABILITIES),
        )
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / 'auth.json'
            auth.write_text('{}', encoding='utf-8')
            for role, no_tools in (('task_verifier', False), ('test_verifier', True),
                                   ('python-executor', False)):
                with self.subTest(role=role):
                    inspect = Mock(return_value=False)
                    runner = Mock()
                    control = Mock()
                    adapter = DockerCodexAdapter(
                        image=image, proxy_image='proxy@sha256:' + 'b' * 64,
                        codex_version='1.2.3', auth_file=auth,
                        isolation_capabilities=capabilities, image_validator=inspect,
                        runner=runner, docker_runner=control,
                    )
                    recipient = 'executor' if role == 'python-executor' else role
                    request = adapters.bind_launch_request(LaunchRequest(
                        role=role, task_id='TC-11', prompt='permitted content and evidence',
                        report_path=Path('report.md'), read_only=True, no_tools=no_tools,
                        allowed_scope=('src/**',), recipient_role=recipient,
                        bundle_digest='a' * 64,
                        composition=LaunchComposition(recipient, 'a' * 64, ('src/**',), ()),
                    ))
                    with self.assertRaises(AdapterError) as raised:
                        adapter.launch(request)
                    self.assertEqual(raised.exception.code, 'stack-isolation-unsupported')
                    inspect.assert_not_called()
                    runner.assert_not_called()
                    control.assert_not_called()

    def test_role_name_cannot_authorize_a_live_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / 'auth.json'
            auth.write_text('{}', encoding='utf-8')
            inspect = Mock(return_value=False)
            runner = Mock()
            adapter = DockerCodexAdapter(
                image='image@sha256:' + 'a' * 64,
                proxy_image='proxy@sha256:' + 'b' * 64,
                codex_version='1.2.3', auth_file=auth,
                image_validator=inspect, runner=runner,
            )
            request = LaunchRequest(
                role='runner-live-isolation-probe', task_id='TC-11',
                prompt='untrusted ordinary launch', report_path=Path('report.md'),
            )
            with self.assertRaises(AdapterError) as raised:
                adapter.launch(request)
            self.assertEqual(raised.exception.code, 'stack-isolation-unsupported')
            inspect.assert_not_called()
            runner.assert_not_called()


class ComposedToolBoundaryTests(unittest.TestCase):
    def test_concrete_tools_cannot_exceed_composed_grants(self) -> None:
        for adapter_type in (ClaudeAdapter, CodexAdapter):
            with self.subTest(adapter=adapter_type.__name__):
                runner = Mock()
                adapter = adapter_type(executable='unused', runner=runner)
                request = LaunchRequest(
                    role='python-executor', task_id='TC-11', prompt='work',
                    report_path=Path('report.md'), role_grant=('read',),
                    tools=('Read', 'Bash'), recipient_role='executor',
                    bundle_digest='a' * 64,
                    composition=LaunchComposition('executor', 'a' * 64, (), ('read',)),
                )
                with self.assertRaises(AdapterError) as raised:
                    adapter.launch(request)
                self.assertEqual(raised.exception.code, 'role-bundle-substitution')
                runner.assert_not_called()


class BoundRequestTests(unittest.TestCase):
    def test_generic_executor_cannot_replace_the_selected_stack_executor(self) -> None:
        request = LaunchRequest(
            role='executor', task_id='TC-11', prompt='Python bundle',
            report_path=Path('report.md'), recipient_role='executor',
            bundle_digest='a' * 64,
            composition=LaunchComposition(
                'executor', 'a' * 64, (), (), executor_identity='python-executor',
            ),
        )
        with self.assertRaises(AdapterError) as raised:
            adapters.bind_launch_request(request)
        self.assertEqual(raised.exception.code, 'role-bundle-substitution')

    def test_selected_executor_identity_cannot_authorize_a_verifier(self) -> None:
        for role in ('task_verifier', 'test_verifier', 'task-verifier', 'test-verifier'):
            with self.subTest(role=role):
                request = LaunchRequest(
                    role=role, task_id='TC-11', prompt='executor content',
                    report_path=Path('report.md'), read_only=True,
                    recipient_role='executor', bundle_digest='a' * 64,
                    composition=LaunchComposition(
                        'executor', 'a' * 64, (), (), executor_identity=role,
                    ),
                )
                with self.assertRaises(AdapterError) as raised:
                    adapters.bind_launch_request(request)
                self.assertEqual(raised.exception.code, 'role-bundle-substitution')

    def test_semantic_executor_bundle_binds_each_selected_executor_identity(self) -> None:
        for identity in ('general-purpose', 'release-manager', 'docs-maintainer'):
            with self.subTest(identity=identity):
                request = LaunchRequest(
                    role=identity, task_id='TC-11', prompt='reviewed skill content',
                    report_path=Path('report.md'), role_grant=('read',), tools=('Read',),
                    recipient_role='executor', bundle_digest='a' * 64,
                    composition=LaunchComposition(
                        'executor', 'a' * 64, (), ('read',), executor_identity=identity,
                    ),
                )
                bound = adapters.bind_launch_request(request)
                with self.assertRaises(AdapterError) as raised:
                    ClaudeAdapter(executable='unused').launch(bound)
                self.assertEqual(raised.exception.code, 'stack-isolation-unsupported')

    def test_executor_bundle_cannot_be_substituted_into_a_verifier_launch(self) -> None:
        request = LaunchRequest(
            role='task_verifier', task_id='TC-11', prompt='reviewed skill content',
            report_path=Path('report.md'), read_only=True, role_grant=('read',), tools=('Read',),
            recipient_role='executor', bundle_digest='a' * 64,
            composition=LaunchComposition(
                'executor', 'a' * 64, (), ('read',), executor_identity='general-purpose',
            ),
        )
        with self.assertRaises(AdapterError) as raised:
            adapters.bind_launch_request(request)
        self.assertEqual(raised.exception.code, 'role-bundle-substitution')

    def test_selected_executor_identity_cannot_be_substituted(self) -> None:
        request = LaunchRequest(
            role='python-executor', task_id='TC-11', prompt='reviewed skill content',
            report_path=Path('report.md'), role_grant=('read',), tools=('Read',),
            recipient_role='executor', bundle_digest='a' * 64,
            composition=LaunchComposition(
                'executor', 'a' * 64, (), ('read',), executor_identity='general-purpose',
            ),
        )
        with self.assertRaises(AdapterError) as raised:
            adapters.bind_launch_request(request)
        self.assertEqual(raised.exception.code, 'role-bundle-substitution')

    def test_bound_selected_executor_identity_cannot_be_rewritten(self) -> None:
        request = LaunchRequest(
            role='python-executor', task_id='TC-11', prompt='reviewed skill content',
            report_path=Path('report.md'), role_grant=('read',), tools=('Read',),
            recipient_role='executor', bundle_digest='a' * 64,
            composition=LaunchComposition(
                'executor', 'a' * 64, (), ('read',), executor_identity='python-executor',
            ),
        )
        bound = adapters.bind_launch_request(request)
        rewritten = replace(
            bound,
            composition=replace(bound.composition, executor_identity='python_executor'),
        )
        with self.assertRaises(AdapterError) as raised:
            ClaudeAdapter(executable='unused').launch(rewritten)
        self.assertEqual(raised.exception.code, 'role-bundle-substitution')

    def test_bound_output_destinations_cannot_be_redirected(self) -> None:
        bound = adapters.bind_launch_request(LaunchRequest(
            role='python-executor', task_id='TC-11', prompt='reviewed content',
            report_path=Path('reports/executor.md'),
            envelope_path=Path('reports/status.json'),
            recipient_role='executor', bundle_digest='a' * 64,
            composition=LaunchComposition('executor', 'a' * 64, (), ()),
        ))
        for change in (
            {'report_path': Path('outside/overwrite.md')},
            {'envelope_path': Path('outside/overwrite.json')},
        ):
            for adapter_type in (ClaudeAdapter, CodexAdapter):
                with self.subTest(change=change, adapter=adapter_type.__name__):
                    runner = Mock()
                    with self.assertRaises(AdapterError) as raised:
                        adapter_type(executable='unused', runner=runner).launch(
                            replace(bound, **change))
                    self.assertEqual(raised.exception.code, 'role-bundle-substitution')
                    runner.assert_not_called()
        self.assertEqual(adapters.bind_launch_request(bound), bound)

    def test_bound_request_denies_later_security_field_substitution(self) -> None:
        original = LaunchRequest(
            role='python-executor', task_id='TC-11', prompt='reviewed skill content',
            report_path=Path('report.md'), role_grant=('read',), tools=('Read',),
            allowed_scope=('src/**',), recipient_role='executor', bundle_digest='a' * 64,
            composition=LaunchComposition('executor', 'a' * 64, ('src/**',), ('read',)),
        )
        bound = adapters.bind_launch_request(original)
        for change in (
            {'role': 'rust-executor'}, {'prompt': 'substituted skill content'},
            {'working_root': '/another-stack'}, {'required_input_dirs': ('/ambient',)},
            {'task_id': 'another-task'}, {'allowed_tools': ('Read',)},
        ):
            for adapter_type in (ClaudeAdapter, CodexAdapter):
                with self.subTest(change=change, adapter=adapter_type.__name__):
                    runner = Mock()
                    with self.assertRaises(AdapterError) as raised:
                        adapter_type(executable='unused', runner=runner).launch(replace(bound, **change))
                    self.assertEqual(raised.exception.code, 'role-bundle-substitution')
                    runner.assert_not_called()
        # An unchanged valid binding passes identity checks and reaches the capability gate.
        with self.assertRaises(AdapterError) as raised:
            ClaudeAdapter(executable='unused').launch(bound)
        self.assertEqual(raised.exception.code, 'stack-isolation-unsupported')


class AdapterInputBindingTests(unittest.TestCase):
    def test_adapter_cannot_append_ambient_read_roots_after_binding(self) -> None:
        from feature_pipeline.ports.adapters import IsolationCapabilityProof
        for adapter_type, name, defaults in (
            (ClaudeAdapter, 'claude', adapters.CLAUDE_ISOLATION_CAPABILITIES),
            (CodexAdapter, 'codex', adapters.CODEX_ISOLATION_CAPABILITIES),
        ):
            surface = 'claude -p' if name == 'claude' else 'codex exec'
            capabilities = replace(
                defaults, **{'supports_' + token: True for token in adapters.STRICT_ISOLATION_CAPABILITIES},
                runtime=name, cli_surface=surface, observed_version='fixture-only',
                isolation_proofs=tuple(IsolationCapabilityProof(
                    token, name, name, surface, 'fixture-only', 'test-only evidence',
                ) for token in adapters.STRICT_ISOLATION_CAPABILITIES),
            )
            runner = Mock(return_value=adapters.CompletedProcess(0, '', ''))
            request = adapters.bind_launch_request(LaunchRequest(
                role='python-executor', task_id='TC-11', prompt='reviewed content',
                report_path=Path('report.md'), role_grant=('read',), tools=('Read',),
                recipient_role='executor', bundle_digest='a' * 64,
                composition=LaunchComposition('executor', 'a' * 64, (), ('read',)),
            ))
            with self.subTest(adapter=name):
                with self.assertRaises(AdapterError) as raised:
                    adapter_type(
                        executable=name, runner=runner, isolation_capabilities=capabilities,
                        required_input_dirs={'TC-11': ('/ambient',)},
                    ).launch(request)
                self.assertEqual(raised.exception.code, 'role-bundle-substitution')
                runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
