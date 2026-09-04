"""CLI-02 bootstrap boundary tests."""

from __future__ import annotations

import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from feature_pipeline.ports.adapters import (
    AdapterUnavailable,
    IncompatibleAdapter,
    READ_ONLY,
    RESUME,
)
from feature_pipeline.cli import use_cases
from feature_pipeline.bootstrap import (
    AdapterFactory,
    AdapterRuntime,
    build_bootstrap,
    codex_factory,
)
from pipeline_core.adapters import CodexAdapter, LaunchRequest, LaunchResult


class FakeAdapter:
    name = "claude"

    def available(self) -> bool:
        return True

    def launch(self, request: LaunchRequest) -> LaunchResult:
        return LaunchResult(0, "ok")


class CliBootstrapBoundaryTests(unittest.TestCase):
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
            with self.assertRaises(IncompatibleAdapter):
                composition.adapter_registry.require("claude", (RESUME,))
            self.assertEqual(
                composition.adapter_registry.require("claude", (READ_ONLY,)).name,
                "claude",
            )

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


if __name__ == "__main__":
    unittest.main()
