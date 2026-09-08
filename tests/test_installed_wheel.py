"""PKG-03 — installed-wheel behaviour and its CI contract.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 1),
``docs/plans/tasks/PKG-03_installed-wheel-ci.md``.

These tests drive :mod:`tests.installed_wheel` — build a wheel with ``uv build``, install
it into a throwaway ``uv`` environment, and invoke the ``feature-pipeline`` console command
and ``python -m unittest`` discovery from an unrelated working directory:

* **AC-1** — the wheel installs and the console command starts with no source-tree path
  injection (``pipeline_core`` / ``feature_pipeline`` resolve from ``site-packages``; the
  historical ``schemas`` compatibility shim was removed in DOC-02).
* **AC-2** — a Windows job and a Linux job run the *same* isolated-install contract; the
  supported matrix lives in one place (:data:`installed_wheel.SUPPORTED_MATRIX`) and the
  committed workflow reads it.
* **AC-3** — ``unittest`` discovery passes both from the project root (this suite) and
  against a copy of the contract module that can only import through the wheel.

``uv`` is a hard requirement of the recipe, not an optional nicety: if it is missing the
recipe raises and this module **errors** rather than skipping, so a broken toolchain stays
visible (PKG-03 Implementation Notes).

Standard library only.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests import installed_wheel
from tests.installed_wheel import (
    SUPPORTED_MATRIX,
    AcceptanceEvidence,
    collect_evidence,
    uv_available,
)
from tests._umbrella import require_umbrella, umbrella_root

_REPO_ROOT = umbrella_root()

# Build + install once for the whole module — the recipe is deterministic and the runtime
# has no dependencies, but a venv per test method is still wasteful.
_EVIDENCE: AcceptanceEvidence | None = None
_EVIDENCE_ERROR: BaseException | None = None


def setUpModule() -> None:
    global _EVIDENCE, _EVIDENCE_ERROR
    try:
        _EVIDENCE = collect_evidence()
    except BaseException as exc:  # surfaced by every test below, never silently skipped
        _EVIDENCE_ERROR = exc


def _evidence() -> AcceptanceEvidence:
    if _EVIDENCE_ERROR is not None:
        raise AssertionError(
            f"the isolated-install recipe did not run: {_EVIDENCE_ERROR}"
        ) from _EVIDENCE_ERROR
    assert _EVIDENCE is not None
    return _EVIDENCE


class ToolchainContract(unittest.TestCase):
    def test_uv_is_available(self) -> None:
        # The recipe needs uv; make its absence an explicit failure, not a skip.
        self.assertTrue(uv_available(), "uv must be installed to build and install the wheel")


class IsolatedInstall(unittest.TestCase):
    """AC-1 — wheel installs, console command starts, no path injection."""

    def _check(self, name: str) -> installed_wheel.Check:
        for check in _evidence().checks:
            if check.name == name:
                return check
        raise AssertionError(f"the harness recorded no {name!r} check")

    def test_wheel_builds_and_installs(self) -> None:
        self.assertTrue(_evidence().wheel_name.endswith(".whl"))

    def test_namespaces_import_from_the_install(self) -> None:
        check = self._check("namespaces_import_from_site_packages")
        self.assertTrue(check.ok, check.detail)

    def test_no_source_tree_on_sys_path(self) -> None:
        check = self._check("no_source_tree_on_path")
        self.assertTrue(check.ok, check.detail)

    def test_console_command_starts_from_an_unrelated_directory(self) -> None:
        check = self._check("console_help_exit_zero")
        self.assertTrue(check.ok, check.detail)

    def test_console_command_keeps_the_push_denial(self) -> None:
        check = self._check("console_push_denied")
        self.assertTrue(check.ok, check.detail)

    def test_root_discovery_still_fails_closed(self) -> None:
        check = self._check("root_discovery_fail_closed")
        self.assertTrue(check.ok, check.detail)


class InstalledPackageDiscovery(unittest.TestCase):
    """AC-3 — test discovery passes against the wheel-only install."""

    def test_contract_module_discovers_and_passes_from_the_install(self) -> None:
        check = next(
            c for c in _evidence().checks if c.name == "installed_package_discovery"
        )
        self.assertTrue(check.ok, check.detail)

    def test_contract_module_also_runs_in_this_source_tree_suite(self) -> None:
        # The same module is collected by `uv run python -m unittest discover -s tests`.
        from tests import test_installed_contract

        loaded = unittest.defaultTestLoader.loadTestsFromModule(test_installed_contract)
        self.assertGreater(loaded.countTestCases(), 0)


class RedactedEvidence(unittest.TestCase):
    """AGENTS.md rule 6 — no host-specific absolute path reaches the committed doc."""

    def test_rendered_markdown_has_no_home_or_repo_path(self) -> None:
        rendered = installed_wheel.render_markdown(_evidence())
        for leaked in (str(Path.home()), str(_REPO_ROOT)):
            self.assertNotIn(leaked, rendered, "host path leaked into the evidence doc")

    def test_committed_doc_block_is_redacted(self) -> None:
        doc = _REPO_ROOT / "docs" / "validation" / "installed-package.md"
        if doc.is_file():
            text = doc.read_text(encoding="utf-8")
            self.assertNotIn(str(Path.home()), text)


class CiMatrixContract(unittest.TestCase):
    """AC-2 / UGA-06 — Windows and Linux CI run the same contract, and both the supported
    matrix and the gate argv are read from ``ci/gates.toml`` through the ``ci/run.py`` driver,
    never transcribed into the umbrella workflow YAML."""

    def setUp(self) -> None:
        # installed-package.yml is an umbrella-repo workflow; a standalone
        # feature-pipeline-skill checkout never carries it.
        require_umbrella(installed_wheel.CI_WORKFLOW)
        self.workflow = _REPO_ROOT / installed_wheel.CI_WORKFLOW
        if not self.workflow.is_file():
            self.fail(f"missing CI workflow {installed_wheel.CI_WORKFLOW}")
        self.text = self.workflow.read_text(encoding="utf-8")
        self.gate = installed_wheel.manifest_consumer_gate()

    def test_manifest_owns_the_supported_matrix(self) -> None:
        self.assertEqual(set(self.gate.os), {"ubuntu-latest", "windows-latest"})
        self.assertEqual(set(self.gate.python), {"3.11", "3.12", "3.13"})

    def test_supported_matrix_is_derived_from_the_manifest(self) -> None:
        self.assertEqual(
            SUPPORTED_MATRIX,
            {image: list(self.gate.python) for image in self.gate.os},
        )
        self.assertIn("windows-latest", SUPPORTED_MATRIX)
        self.assertIn("ubuntu-latest", SUPPORTED_MATRIX)

    def test_workflow_expands_its_matrix_from_the_driver(self) -> None:
        self.assertIn("ci.run list --json --group consumer", self.text)
        self.assertIn(
            "fromJSON(needs.prepare-consumer-gates.outputs.include)", self.text
        )

    def test_workflow_runs_the_gate_only_through_the_driver(self) -> None:
        self.assertIn("ci.run run ${{ matrix.gate.id }}", self.text)
        for embedded in (
            "unittest discover -s tests -t .",
            "unittest -v tests.test_installed_wheel",
            "python -m tests.installed_wheel --json",
        ):
            self.assertNotIn(embedded, self.text)

    def test_workflow_binds_the_exact_gitlink_sha(self) -> None:
        self.assertIn("git rev-parse HEAD:feature-pipeline-skill", self.text)
        self.assertIn(
            '--expected-source-sha "${{ steps.gitlink.outputs.sha }}"', self.text
        )

    def test_workflow_sets_an_explicit_submodule_source_root(self) -> None:
        self.assertIn(
            '--source-root "$GITHUB_WORKSPACE/feature-pipeline-skill"', self.text
        )
        self.assertNotIn("working-directory:", self.text)

    def test_evidence_is_not_collected_unconditionally(self) -> None:
        self.assertNotIn("always()", self.text)

    def test_workflow_uses_uv(self) -> None:
        self.assertIn("uv", self.text)


if __name__ == "__main__":
    unittest.main()
