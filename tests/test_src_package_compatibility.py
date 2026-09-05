"""PKG-02 — import and launcher compatibility for the ``src`` package facade.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 1),
``docs/adr/002-installable-package-layout.md``.

PKG-02 created ``src/feature_pipeline`` and moved the colliding top-level ``schemas``
namespace under it behind a temporary shim. DOC-02 removed that shim after its one
deprecation window (``docs/adr/007-compatibility-and-versioning-policy.md`` §8); the
historical-spelling assertions that used to live here moved to
``tests/test_no_legacy_schemas_namespace.py``, which now pins the *absence* of the old
namespace. What remains here:

* AC-1 — ``import feature_pipeline`` / ``feature_pipeline.contracts`` resolve with **no**
  source-tree ``sys.path`` injection (the package is picked up from the editable install
  that ``uv run`` materialises), and the facade exposes an explicit ``__all__``.
* AC-2 — the ``scripts/run_pipeline.py`` launcher keeps its argv, exit codes and ``--push``
  denial byte-for-byte.
* AC-3 — the move is pure relocation: no fixture is touched.

Standard library only.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_CORE_ROOT = Path(__file__).resolve().parents[1]

# The curated public surface the pre-move ``schemas/__init__.py`` exported, and that
# ``feature_pipeline.contracts`` must still expose now that it is the only spelling.
_HISTORICAL_SCHEMAS_ALL = {
    "AcceptanceCriterionSpec", "CommandSpec", "LogicalPaths", "Profile", "ProfileRegistry",
    "RunState", "SchemaError", "TaskMetadata", "TaskRoute", "TaskSpec", "ToolStage",
    "Verdict", "ensure_no_role_escalation", "load_profile", "validate_acceptance_criteria",
    "validate_relative_path",
}

# Names imported from ``schemas.contracts`` by former call sites that are *not* in the
# curated ``__all__`` — ``feature_pipeline.contracts`` must keep exposing them too.
_CONTRACTS_EXTRA_NAMES = {"DIFF_POLICIES", "TASK_TYPES", "SCHEMA_VERSION"}


class NewPackageImports(unittest.TestCase):
    """AC-1 — the target namespace imports without a path hack and documents its API."""

    def test_feature_pipeline_package_imports(self) -> None:
        import feature_pipeline

        # Resolves to the src-layout package the wheel declares, not a stray copy.
        self.assertEqual(
            Path(feature_pipeline.__file__).resolve().parent,
            (_CORE_ROOT / "src" / "feature_pipeline").resolve(),
        )

    def test_imports_from_the_install_without_a_path_hack(self) -> None:
        # An isolated interpreter (-I: no PYTHONPATH, no user site, no implicit cwd) run
        # from an unrelated directory can only find the package through the install.
        with tempfile.TemporaryDirectory() as elsewhere:
            done = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import feature_pipeline, feature_pipeline.contracts; print('ok')"],
                capture_output=True, text=True, cwd=elsewhere,
            )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "ok")

    def test_facade_exposes_explicit_public_api(self) -> None:
        import feature_pipeline

        self.assertTrue(hasattr(feature_pipeline, "__all__"), "the facade must declare __all__")
        self.assertTrue(feature_pipeline.__all__, "__all__ must not be empty")
        for name in feature_pipeline.__all__:
            self.assertTrue(hasattr(feature_pipeline, name),
                            f"{name} is advertised in __all__ but not importable")

    def test_contracts_submodule_is_the_canonical_home(self) -> None:
        import feature_pipeline.contracts as contracts

        self.assertEqual(
            contracts.__name__, "feature_pipeline.contracts",
            "the contracts module must live under the feature_pipeline namespace",
        )
        for name in _HISTORICAL_SCHEMAS_ALL | _CONTRACTS_EXTRA_NAMES:
            self.assertTrue(hasattr(contracts, name), f"feature_pipeline.contracts lost {name}")


class LauncherCompatibility(unittest.TestCase):
    """AC-2 — scripts/run_pipeline.py keeps its argv / exit / denial behaviour."""

    _LAUNCHER = _CORE_ROOT / "scripts" / "run_pipeline.py"

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # ``-S``: skip site initialisation so the editable install's ``.pth`` is NOT
        # processed. The launcher must make the core importable from its own ``sys.path``
        # handling alone — i.e. it keeps working for a plain ``python run_pipeline.py`` with
        # nothing installed, exactly as before PKG-02.
        return subprocess.run(
            [sys.executable, "-S", str(self._LAUNCHER), *args],
            capture_output=True, text=True, cwd=_CORE_ROOT.parent,
        )

    def test_help_exits_zero(self) -> None:
        done = self._run("--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("usage", done.stdout.lower())

    def test_push_is_denied_with_exit_one(self) -> None:
        done = self._run("--push")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(
            done.stderr.strip(),
            "push is denied at this stage and the runner has no push code path.",
        )


class MoveIsPureRelocation(unittest.TestCase):
    """AC-3 — no public contract or fixture change is hidden in the move."""

    def test_fixture_tree_untouched(self) -> None:
        # The PKG-02 move must not modify or delete any pre-existing fixture. Later tasks
        # may legitimately *add* fixtures (DS-02 adds ``fixtures/state/`` in its Allowed
        # scope), so brand-new untracked paths are not a move regression.
        done = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no", "--", "fixtures"],
            capture_output=True, text=True, cwd=_CORE_ROOT,
        )
        touched = [
            line for line in done.stdout.splitlines() if not line.startswith("A ")
        ]
        self.assertEqual(touched, [], "PKG-02 must not modify or delete fixtures/")


if __name__ == "__main__":
    unittest.main()
