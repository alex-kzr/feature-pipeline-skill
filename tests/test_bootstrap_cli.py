"""CLI-02 bootstrap boundary tests."""

from __future__ import annotations

import inspect
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest

from feature_pipeline.ports.adapters import (
    CLAUDE,
    CODEX,
    AdapterUnavailable,
    IncompatibleAdapter,
    READ_ONLY,
    RESUME,
    STRICT_ISOLATION_CAPABILITIES,
)
from feature_pipeline.cli import use_cases
from feature_pipeline.bootstrap import (
    AdapterFactory,
    AdapterRuntime,
    build_bootstrap,
    docker_codex_factory,
    codex_factory,
    docker_capabilities_from_durable_proof,
)
from feature_pipeline.cli.commands import RunCommand
from feature_pipeline.cli.parser import build_parser
from pipeline_core.adapters import CodexAdapter, DockerCodexAdapter, LaunchRequest, LaunchResult
from pipeline_core.state import Run
from pipeline_core.verification import RunnerOwnedIsolationProofVerifier
from tests.support.isolation import proven_isolation_capabilities


class FakeAdapter:
    name = "claude"

    def available(self) -> bool:
        return True

    def launch(self, request: LaunchRequest) -> LaunchResult:
        return LaunchResult(0, "ok")


class CliBootstrapBoundaryTests(unittest.TestCase):
    def test_typed_docker_codex_runtime_controls_select_pinned_runtime(self) -> None:
        command = RunCommand.from_args(build_parser().parse_args([
            "--adapter", "codex", "--codex-runtime", "docker",
            "--docker-codex-image", "node@sha256:b6f26b36c8ff49624cfdac716b8ea1138d606df02586a77d364bb5536a634f85",
            "--docker-proxy-image", "python@sha256:1a63a53928ce53d2b0baf08092a703f4840ac5dfbd61fd48802dbf48e08c801e",
            "--docker-codex-version", "0.1.0",
            "--docker-codex-auth-file", "C:/runner/auth.json",
        ]))

        self.assertEqual(command.codex_runtime, "docker")
        self.assertEqual(command.docker_codex_image, "node@sha256:b6f26b36c8ff49624cfdac716b8ea1138d606df02586a77d364bb5536a634f85")
        self.assertEqual(command.docker_proxy_image, "python@sha256:1a63a53928ce53d2b0baf08092a703f4840ac5dfbd61fd48802dbf48e08c801e")
        self.assertEqual(command.docker_codex_version, "0.1.0")
        self.assertEqual(command.docker_codex_auth_file, "C:/runner/auth.json")
    def test_console_entry_point_depends_on_cli_and_bootstrap_apis_only(self) -> None:
        runner = Path(use_cases.__file__).parents[3] / "pipeline_core" / "runner_cli.py"
        source = runner.read_text(encoding="utf-8")

        self.assertIsNone(
            re.search(r"^\s*(?:from|import)\s+pipeline_core", source, re.MULTILINE)
        )

    def test_cli_modules_depend_on_bootstrap_not_pipeline_core(self) -> None:
        cli_root = Path(use_cases.__file__).parent
        for module in ("use_cases.py", "renderers.py"):
            source = (cli_root / module).read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"^\s*(?:from|import)\s+pipeline_core", source, re.MULTILINE),
                module,
            )

    def test_cli_use_cases_do_not_import_concrete_claude_or_private_execution_helpers(
        self,
    ) -> None:
        source = inspect.getsource(use_cases)

        self.assertNotIn("ClaudeAdapter", source)
        self.assertNotIn("from pipeline_core.execution import", source)
        self.assertNotIn("_resolve_attestation", source)
        self.assertNotIn("_validate_attestation_scope", source)

    def test_preview_use_case_compiles_with_bootstrap_registry(self) -> None:
        source = inspect.getsource(use_cases.run_command)

        self.assertIn("build_bootstrap", source)
        self.assertIn("adapters=composition.adapter_registry", source)

    def test_bootstrap_registry_uses_factory_availability_and_capabilities(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            factory = AdapterFactory(
                name="claude",
                create=lambda _runtime: FakeAdapter(),
                available=lambda: False,
                supports_resume=False,
                supports_read_only=True,
                supports_write=True,
            )

            composition = build_bootstrap(root, root / ".agents", root, (factory,))

            with self.assertRaises(AdapterUnavailable):
                composition.adapter_registry.resolve(None)
            executor, _launchers, environment = composition.make_execute_adapters()
            with self.assertRaises(IncompatibleAdapter):
                composition.adapter_registry.require("claude", (RESUME,))
            self.assertEqual(
                composition.adapter_registry.require("claude", (READ_ONLY,)).name,
                "claude",
            )
            self.assertIsInstance(executor, FakeAdapter)
            self.assertEqual(environment, {"claude": False, "codex": False})

    def test_bootstrap_can_substitute_a_fake_adapter_factory(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            seen_runtime: list[AdapterRuntime] = []
            fake = FakeAdapter()

            def create(runtime: AdapterRuntime) -> FakeAdapter:
                seen_runtime.append(runtime)
                return fake

            composition = build_bootstrap(
                root,
                root / ".agents",
                root / "core",
                (
                    AdapterFactory(
                        name="claude",
                        create=create,
                        available=lambda: True,
                        supports_resume=True,
                        supports_read_only=True,
                        supports_write=True,
                    ),
                ),
            )

            executor, launchers, environment = composition.make_execute_adapters()

            self.assertIs(executor, fake)
            self.assertIs(launchers.task, fake)
            self.assertIs(launchers.test, fake)
            self.assertEqual(environment, {"claude": True, "codex": False})
            self.assertEqual(seen_runtime[0].project_dir, root)
            self.assertEqual(seen_runtime[0].core_root, root / "core")
            self.assertEqual(
                seen_runtime[0].scope_roots,
                (),
            )

    def test_bootstrap_uses_profile_logical_paths_for_external_scope_roots(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            agents_root = Path(directory) / "agents"
            core_root = Path(directory) / "core"
            seen_runtime: list[AdapterRuntime] = []

            composition = build_bootstrap(
                root,
                agents_root,
                core_root,
                (AdapterFactory(
                    name="claude",
                    create=lambda runtime: seen_runtime.append(runtime) or FakeAdapter(),
                    available=lambda: True,
                    supports_resume=True,
                    supports_read_only=True,
                    supports_write=True,
                ),),
                logical_paths={"agents": "shared/agents", "core": "shared/core"},
            )

            composition.make_execute_adapters()

        self.assertEqual(
            seen_runtime[0].scope_roots,
            (("shared/agents", agents_root.resolve()), ("shared/core", core_root.resolve())),
        )

    def test_bootstrap_reuses_one_registry_for_compile_and_adapter_creation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            availability_checks = 0

            def available() -> bool:
                nonlocal availability_checks
                availability_checks += 1
                return True

            composition = build_bootstrap(
                root,
                root / ".agents",
                root / "core",
                (
                    AdapterFactory(
                        name="claude",
                        create=lambda _runtime: FakeAdapter(),
                        available=available,
                        supports_resume=True,
                        supports_read_only=True,
                        supports_write=True,
                    ),
                ),
            )

            registry = composition.adapter_registry
            composition.make_execute_adapters()

        self.assertIs(registry, composition.adapter_registry)
        self.assertEqual(availability_checks, 1)

    def test_bootstrap_passes_the_evidence_bound_isolation_record_to_the_factory(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            seen_runtime: list[AdapterRuntime] = []
            proof = proven_isolation_capabilities("claude")
            composition = build_bootstrap(
                root,
                root / ".agents",
                root / "core",
                (AdapterFactory(
                    name="claude",
                    create=lambda runtime: seen_runtime.append(runtime) or FakeAdapter(),
                    available=lambda: True,
                    supports_resume=True,
                    supports_read_only=True,
                    supports_write=True,
                    isolation_capabilities=proof,
                ),),
            )
            resolved = composition.adapter_registry.resolve("claude")
            composition.make_execute_adapters("claude")

        self.assertTrue(resolved.has("subprocess_isolated"))
        self.assertIs(seen_runtime[0].isolation_capabilities, resolved)

    def test_production_composition_passes_each_adapter_its_own_unproven_capabilities(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            composition = build_bootstrap(root, root / ".agents", root / "core")

            for name in (CLAUDE, CODEX):
                with self.subTest(adapter=name):
                    resolved = composition.adapter_registry.select(name)
                    executor, _launchers, _environment = composition.make_execute_adapters(name)

                    self.assertIs(executor._isolation_capabilities, resolved)
                    self.assertFalse(any(resolved.has(token) for token in STRICT_ISOLATION_CAPABILITIES))

    def test_production_composition_keeps_diagnostic_probe_records_non_capability_bearing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (CLAUDE, CODEX):
                record = root / ".pipeline" / "isolation-probes" / f"{name}.json"
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text('{"verification_status":"PASS"}', encoding="utf-8")
            composition = build_bootstrap(root, root / ".agents", root / "core")

            for name in (CLAUDE, CODEX):
                with self.subTest(adapter=name):
                    resolved = composition.adapter_registry.select(name)
                    self.assertFalse(any(resolved.has(token) for token in STRICT_ISOLATION_CAPABILITIES))

    def test_codex_factory_declares_an_available_non_resuming_adapter(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            composition = build_bootstrap(
                root,
                root / ".agents",
                root / "core",
                (codex_factory(resolver=lambda: "fake-codex"),),
            )

            capabilities = composition.adapter_registry.resolve("codex")
            executor, _launchers, environment = composition.make_execute_adapters("codex")

        self.assertFalse(capabilities.supports_resume)
        self.assertIsInstance(executor, CodexAdapter)
        self.assertEqual(environment, {"codex": True})

    def test_digest_pinned_container_factory_is_explicit_and_never_the_default_codex_path(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth.json"
            auth.write_text("test-only", encoding="utf-8")
            image = "example.invalid/codex@sha256:" + "a" * 64
            composition = build_bootstrap(
                root, root / ".agents", root / "core",
                (docker_codex_factory(
                    image=image, proxy_image="example.invalid/python@sha256:" + "b" * 64,
                    codex_version="0.1.0", auth_file=auth, image_validator=lambda *_: True,
                ),),
            )
            executor, _launchers, _environment = composition.make_execute_adapters("codex")

        self.assertIsInstance(executor, DockerCodexAdapter)

    def test_durable_docker_proof_grants_strict_capabilities_only_for_its_exact_binding(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("prompt", encoding="utf-8")
            run = Run.create("tc11", prompt, None, root / "runs" / "tc11", root)
            task = run.add_task("TC-11")
            task.adapter = "codex"
            task.task_contract_digest = "contract-20"
            image = "example.invalid/codex@sha256:" + "a" * 64
            binding = DockerCodexAdapter.probe_binding(
                image=image, proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0", auth_file=root / "auth.json",
            )
            proof = {
                "schema_version": 1, "task_id": "TC-11", "attempt_id": "probe-20",
                "role": "runner-live-isolation-probe", "disposition": "CONTAINMENT_PROVEN",
                "reason": "all runner-visible containment observations passed",
                "observations": {name: True for name in (
                    "exact_controls", "allowed_write", "parent_read_attempted",
                    "parent_read_contained", "child_read_attempted", "child_read_contained",
                    "nested_surface_absent", "network_contained", "process_contained",
                )},
                "binding": binding, "cleanup_removed": True, "exit_zero": True,
                "failure_class": None,
            }
            artifact = run.run_dir / "reports" / "TC-11" / "live-probe.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(json.dumps(proof), encoding="utf-8")
            run.live_probe_evidence.append({
                "schema_version": 1, "task_id": "TC-11", "run_id": run.run_id,
                "task_contract_digest": "contract-20", "attempt_id": "probe-20",
                "adapter": "codex", "cli_version": binding["observed_version"],
                "role": "runner-live-isolation-probe", "bundle_digest": "bundle-20",
                "allowed_scope": [], "grants": [], "timeout_s": 1.0, "max_attempts": 1,
                "started_at": "2026-09-20T00:00:00Z", "ended_at": "2026-09-20T00:00:01Z",
                "disposition": "CONTAINMENT_PROVEN", "reason": proof["reason"], "cleanup": "removed",
                "task_contract_revision": 0, "proof_path": "reports/TC-11/live-probe.json",
                "proof_digest": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            })

            capabilities = docker_capabilities_from_durable_proof(
                run, task_ids=("TC-11",), image=image,
                proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0", auth_file=root / "auth.json",
            )
            self.assertTrue(all(capabilities.has(token) for token in STRICT_ISOLATION_CAPABILITIES))
            (root / "auth.json").write_text("test-only", encoding="utf-8")
            composition = build_bootstrap(
                root, root / ".agents", root / "core", (docker_codex_factory(
                    image=image, proxy_image="example.invalid/python@sha256:" + "b" * 64,
                    codex_version="0.1.0", auth_file=root / "auth.json",
                    isolation_capabilities=capabilities,
                ),),
            )
            selected = composition.adapter_registry.select("codex")
            adapter, launchers, _environment = composition.make_execute_adapters("codex", run=run)
            self.assertTrue(all(selected.has(token) for token in STRICT_ISOLATION_CAPABILITIES))
            self.assertIs(adapter._isolation_capabilities, selected)
            self.assertIsInstance(launchers.deterministic_isolation, RunnerOwnedIsolationProofVerifier)

            proof["binding"]["argv_digest"] = "0" * 64
            artifact.write_text(json.dumps(proof), encoding="utf-8")
            self.assertFalse(any(docker_capabilities_from_durable_proof(
                run, task_ids=("TC-11",), image=image,
                proxy_image="example.invalid/python@sha256:" + "b" * 64,
                codex_version="0.1.0", auth_file=root / "auth.json",
            ).has(token) for token in STRICT_ISOLATION_CAPABILITIES))


if __name__ == "__main__":
    unittest.main()
