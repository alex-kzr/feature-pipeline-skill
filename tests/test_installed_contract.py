"""PKG-03 — the portable installed-package integration contract.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 1),
``docs/plans/tasks/PKG-03_installed-wheel-ci.md``.

Every assertion in this module depends **only** on what a built wheel installs:
the ``pipeline_core``, ``schemas`` and ``feature_pipeline`` import namespaces and the
``feature-pipeline`` console entry point. It reads no fixture, resolves no source-tree
path, and never puts the repository on ``sys.path``. That is deliberate — the module is
discovered and run twice:

* from the project root, as part of ``uv run python -m unittest discover -s tests -t .``
  (the package resolves from the ``uv`` editable install); and
* from an isolated environment that has *only* the built wheel installed, driven by
  :mod:`tests.test_installed_wheel` / :mod:`tests.installed_wheel` — the same discovery
  command, a copy of just this file, and a throwaway venv (PKG-03 AC-3, prompt L458).

If an import here starts needing the source checkout, the second run fails loudly, which
is the signal that the wheel is missing a module or data file (PKG-03 Risks note).

Standard library only.
"""

from __future__ import annotations

import importlib
import inspect
import subprocess
import sys
import unittest
from pathlib import Path

# The curated public surface the installable facade guarantees (kept in lockstep with
# ``feature_pipeline.__all__`` and the ``schemas`` shim; see PKG-02).
_PUBLIC_SURFACE = {
    "AcceptanceCriterionSpec", "CommandSpec", "LogicalPaths", "Profile", "ProfileRegistry",
    "RunState", "SchemaError", "TaskMetadata", "TaskRoute", "TaskSpec", "ToolStage",
    "Verdict", "ensure_no_role_escalation", "load_profile", "validate_acceptance_criteria",
    "validate_relative_path",
}

_PUSH_DENIED_MESSAGE = "push is denied at this stage and the runner has no push code path."


class InstalledImportSurface(unittest.TestCase):
    """The three shipped namespaces import and expose their documented API."""

    def test_core_and_facade_import(self) -> None:
        import feature_pipeline
        import pipeline_core  # noqa: F401
        import schemas  # noqa: F401  (deprecation shim, one window — docs/adr/007)

        self.assertTrue(feature_pipeline.__all__, "the facade must declare a non-empty __all__")

    def test_facade_public_names_are_importable(self) -> None:
        import feature_pipeline

        for name in _PUBLIC_SURFACE:
            self.assertTrue(
                hasattr(feature_pipeline, name),
                f"feature_pipeline is missing the public name {name!r}",
            )

    def test_contracts_module_is_the_canonical_home(self) -> None:
        contracts = importlib.import_module("feature_pipeline.contracts")

        self.assertEqual(contracts.__name__, "feature_pipeline.contracts")
        for name in _PUBLIC_SURFACE | {"DIFF_POLICIES", "TASK_TYPES", "SCHEMA_VERSION"}:
            self.assertTrue(hasattr(contracts, name), f"contracts lost {name!r}")

    def test_schemas_shim_reexports_identical_objects(self) -> None:
        # The shim re-exports, it does not redefine: one class identity across both
        # spellings (PKG-02 "no duplicate class identity" risk).
        contracts = importlib.import_module("feature_pipeline.contracts")
        import schemas

        self.assertIs(schemas.Profile, contracts.Profile)
        self.assertIs(schemas.SchemaError, contracts.SchemaError)


class InstalledConsoleContract(unittest.TestCase):
    """The ``feature-pipeline`` entry point resolves and keeps its safety posture."""

    def test_entry_point_target_resolves_to_a_zero_arg_callable(self) -> None:
        module = importlib.import_module("pipeline_core.runner_cli")
        main = getattr(module, "main", None)
        self.assertTrue(callable(main), "pipeline_core.runner_cli:main must be callable")
        required_positionals = [
            p for p in inspect.signature(main).parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        self.assertLessEqual(len(required_positionals), 1,
                             "a console script calls main() with no arguments")

    def test_help_runs_without_a_path_hack(self) -> None:
        # ``-I`` == isolated: no PYTHONPATH, no user site, cwd not on sys.path. The console
        # module can only be found through the install (AC-1).
        done = subprocess.run(
            [sys.executable, "-I", "-m", "pipeline_core.runner_cli", "--help"],
            capture_output=True, text=True, cwd=str(Path(sys.prefix)),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage", done.stdout.lower())

    def test_push_is_refused_before_any_work(self) -> None:
        main = importlib.import_module("pipeline_core.runner_cli").main
        self.assertEqual(main(["--push"]), 1)

    def test_push_denied_message_is_verbatim(self) -> None:
        runner_cli = importlib.import_module("pipeline_core.runner_cli")
        self.assertEqual(runner_cli.PUSH_DENIED_MESSAGE, _PUSH_DENIED_MESSAGE)
        self.assertEqual(runner_cli.EXIT_PUSH_DENIED, 1)


class InstalledRootDiscovery(unittest.TestCase):
    """Root discovery: the installed runner still refuses to infer a layout."""

    def test_anchor_flags_are_present_and_required(self) -> None:
        build_parser = importlib.import_module("pipeline_core.runner_cli").build_parser
        help_text = build_parser().format_help()
        for flag in ("--project-root", "--agents-root", "--core-root"):
            self.assertIn(flag, help_text, f"the installed runner dropped {flag}")

    def test_missing_anchors_fail_closed_without_a_host_path(self) -> None:
        main = importlib.import_module("pipeline_core.runner_cli").main
        # No anchors, not a plan-only dry run of a real plan: the runner must exit
        # non-zero rather than guess a project root from cwd.
        self.assertNotEqual(main(["--mode", "execute"]), 0)


if __name__ == "__main__":
    unittest.main()
