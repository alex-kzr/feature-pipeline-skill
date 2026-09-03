"""PKG-03 — installed-wheel behaviour and its CI contract.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 1),
``docs/plans/tasks/PKG-03_installed-wheel-ci.md``.

These tests drive :mod:`tests.installed_wheel` — build a wheel with ``uv build``, install
it into a throwaway ``uv`` environment, and invoke the ``feature-pipeline`` console command
and ``python -m unittest`` discovery from an unrelated working directory:

* **AC-1** — the wheel installs and the console command starts with no source-tree path
  injection (``pipeline_core`` / ``schemas`` / ``feature_pipeline`` resolve from
  ``site-packages``).
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

_REPO_ROOT = Path(__file__).resolve().parents[2]

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
    """AC-2 — Windows and Linux CI run the same contract off one matrix."""

    def setUp(self) -> None:
        self.workflow = _REPO_ROOT / installed_wheel.CI_WORKFLOW
        if not self.workflow.is_file():
            self.fail(f"missing CI workflow {installed_wheel.CI_WORKFLOW}")
        self.text = self.workflow.read_text(encoding="utf-8")

    def test_matrix_covers_windows_and_linux(self) -> None:
        self.assertIn("windows-latest", SUPPORTED_MATRIX)
        self.assertIn("ubuntu-latest", SUPPORTED_MATRIX)
        for image in SUPPORTED_MATRIX:
            self.assertIn(image, self.text, f"{image} is not referenced by the workflow")

    def test_every_supported_python_is_in_the_workflow(self) -> None:
        for versions in SUPPORTED_MATRIX.values():
            for version in versions:
                self.assertIn(version, self.text,
                              f"Python {version} is in SUPPORTED_MATRIX but not the workflow")

    def test_workflow_runs_root_discovery_and_the_isolated_install(self) -> None:
        # Root discovery: the canonical project-root suite invocation (prompt L458).
        self.assertIn("unittest discover -s tests -t .", self.text)
        # Installed-package integration: the harness this module drives.
        self.assertIn("tests.test_installed_wheel", self.text.replace("/", ".") + self.text)

    def test_workflow_uses_uv(self) -> None:
        self.assertIn("uv", self.text)


if __name__ == "__main__":
    unittest.main()
