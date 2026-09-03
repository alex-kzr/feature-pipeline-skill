"""PKG-02 — import and launcher compatibility for the ``src`` package facade.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 1),
``docs/adr/002-installable-package-layout.md``.

PKG-02 creates ``src/feature_pipeline`` and moves the colliding top-level ``schemas``
namespace under it behind a temporary shim. These tests pin the contract of that move:

* AC-1 — ``import feature_pipeline`` / ``feature_pipeline.contracts`` resolve with **no**
  source-tree ``sys.path`` injection (the package is picked up from the editable install
  that ``uv run`` materialises), and the facade exposes an explicit ``__all__``.
* AC-2 — every historical spelling still works: ``import schemas``,
  ``from schemas import <name>`` and ``from schemas.contracts import <name>``. The shim
  re-exports the *same* objects (``schemas.X is feature_pipeline.contracts.X``), so the
  "dual class identity" risk in the task file cannot occur. The ``scripts/run_pipeline.py``
  launcher keeps its argv, exit codes and ``--push`` denial byte-for-byte.
* AC-3 — the move is pure relocation: the public contract surface exported by the old
  ``schemas`` package is exactly what ``feature_pipeline.contracts`` exports now, and no
  fixture is touched.

Standard library only.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_CORE_ROOT = Path(__file__).resolve().parents[1]

# The curated public surface the pre-move ``schemas/__init__.py`` exported. The move may
# not drop or add a name (AC-3).
_HISTORICAL_SCHEMAS_ALL = {
    "AcceptanceCriterionSpec", "CommandSpec", "LogicalPaths", "Profile", "ProfileRegistry",
    "RunState", "SchemaError", "TaskMetadata", "TaskRoute", "TaskSpec", "ToolStage",
    "Verdict", "ensure_no_role_escalation", "load_profile", "validate_acceptance_criteria",
    "validate_relative_path",
}

# Names imported from ``schemas.contracts`` by current call sites that are *not* in the
# curated ``__all__`` — the module-level shim must keep exposing them too.
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


class HistoricalImportCompatibility(unittest.TestCase):
    """AC-2 — the pre-move spellings keep working and resolve to the same objects."""

    def test_top_level_schemas_still_imports(self) -> None:
        import schemas  # noqa: F401

    def test_from_schemas_import_names(self) -> None:
        from schemas import Profile, SchemaError, load_profile  # noqa: F401

    def test_from_schemas_contracts_import_names(self) -> None:
        from schemas.contracts import DIFF_POLICIES, TASK_TYPES, TaskSpec  # noqa: F401

    def test_shim_reexports_identical_objects(self) -> None:
        import feature_pipeline.contracts as canonical
        import schemas
        from schemas import contracts as shim_contracts

        self.assertIs(schemas.Profile, canonical.Profile)
        self.assertIs(schemas.SchemaError, canonical.SchemaError)
        self.assertIs(shim_contracts.TaskSpec, canonical.TaskSpec)
        self.assertIs(shim_contracts.DIFF_POLICIES, canonical.DIFF_POLICIES)

    def test_no_duplicate_class_identity(self) -> None:
        # The task file's risk: "Dual import paths can create distinct class identities if
        # shims duplicate modules." The shim re-exports objects, it does not redefine them,
        # so an object built through one spelling is an instance of the class named by the
        # other.
        import feature_pipeline.contracts as canonical
        from schemas.contracts import SchemaError as ShimSchemaError

        self.assertIs(ShimSchemaError, canonical.SchemaError)
        raised = None
        try:
            canonical.validate_relative_path("/abs", "field")
        except canonical.SchemaError as exc:  # built by the canonical module
            raised = exc
        self.assertIsInstance(raised, ShimSchemaError)  # caught by the shim's name


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

    def test_public_surface_matches_the_pre_move_schemas_all(self) -> None:
        import feature_pipeline.contracts as contracts
        import schemas

        self.assertEqual(
            set(schemas.__all__), _HISTORICAL_SCHEMAS_ALL,
            "the schemas shim must re-export exactly the pre-move public surface",
        )
        for name in _HISTORICAL_SCHEMAS_ALL:
            self.assertIs(getattr(schemas, name), getattr(contracts, name))

    def test_fixture_tree_untouched(self) -> None:
        done = subprocess.run(
            ["git", "status", "--porcelain", "--", "fixtures"],
            capture_output=True, text=True, cwd=_CORE_ROOT,
        )
        self.assertEqual(done.stdout.strip(), "", "PKG-02 must not modify fixtures/")


if __name__ == "__main__":
    unittest.main()
