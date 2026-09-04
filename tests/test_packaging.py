"""PKG-01 — contract checks over the packaging and quality configuration.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 1). This module reads
``feature-pipeline-skill/pyproject.toml`` and asserts the *contract* PKG-01 introduces
without moving any implementation:

* build metadata, the supported Python floor, and the ``feature-pipeline`` console entry
  point are present and internally valid (AC-1);
* the runtime dependency set stays standard-library only (``docs/adr/003``) — a denial path;
* Ruff, mypy, and test/coverage configuration exist with explicit fixture and generated
  exclusions and an explicit, ratcheted baseline that still lets new violations fail (AC-2);
* the pre-existing Ruff/mypy debt in ``pipeline_core/`` and ``schemas/`` (out of scope for
  PKG-01 per ``docs/adr/002``) is parked behind *named* baselines, not a blanket skip.

No source module is imported for its behaviour; the entry-point target is imported only to
prove the ``module:function`` reference resolves to something callable.

Standard library only.
"""

from __future__ import annotations

import importlib
import inspect
import tomllib
import unittest
from pathlib import Path

_CORE_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _CORE_ROOT / "pyproject.toml"

# The 15 modules that carry the 78 pre-existing mypy errors / 94 Ruff findings catalogued
# in .prompts/feature-pipeline-refactor.md (F-20). PKG-01's Allowed scope excludes
# pipeline_core/** and schemas/**, so this configuration must green the tools by *naming*
# this debt, and each later Phase task clears its slice.
_KNOWN_TYPING_DEBT = {
    "pipeline_core.adapters",
    "pipeline_core.archive",
    "pipeline_core.commands",
    "pipeline_core.dispatch",
    "pipeline_core.execution",
    "pipeline_core.legacy_adapter",
    "pipeline_core.plan_md",
    "pipeline_core.reports",
    "pipeline_core.roles",
    "pipeline_core.runner_cli",
    "pipeline_core.state",
    "pipeline_core.task_files",
    "pipeline_core.verification",
    # `pipeline_core.worktree` — debt cleared in WT-01 (bounded inspector rewrite); the
    # module is now checked at the default mypy level.
    # PKG-02 relocated the top-level `schemas.contracts` under the package namespace.
    "feature_pipeline.contracts",
}


def _load() -> dict:
    with _PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


class PyprojectPresence(unittest.TestCase):
    def test_pyproject_exists_and_parses(self) -> None:
        self.assertTrue(_PYPROJECT.is_file(), f"missing {_PYPROJECT}")
        self.assertIsInstance(_load(), dict)

    def test_uv_lock_is_committed(self) -> None:
        self.assertTrue(
            (_CORE_ROOT / "uv.lock").is_file(),
            "uv.lock must be generated and committed alongside pyproject.toml",
        )


class BuildMetadata(unittest.TestCase):
    """AC-1 — build metadata and console entry point are valid."""

    def setUp(self) -> None:
        self.cfg = _load()

    def test_build_system_declares_a_backend(self) -> None:
        build = self.cfg.get("build-system", {})
        self.assertTrue(build.get("requires"), "build-system.requires must be non-empty")
        self.assertTrue(build.get("build-backend"), "build-system.build-backend must be set")

    def test_project_identity(self) -> None:
        project = self.cfg["project"]
        self.assertEqual(project["name"], "feature-pipeline")
        self.assertTrue(project.get("version"), "an explicit version is required")
        self.assertTrue(project.get("description"))

    def test_supported_python_floor_is_311(self) -> None:
        # docs/adr/003: dataclasses slots=, PEP 604 runtime unions, tomllib, exception groups.
        self.assertEqual(self.cfg["project"]["requires-python"], ">=3.11")

    def test_console_entry_point_resolves_to_a_callable(self) -> None:
        scripts = self.cfg["project"].get("scripts", {})
        self.assertIn("feature-pipeline", scripts, "the feature-pipeline console script is required")
        target = scripts["feature-pipeline"]
        self.assertRegex(target, r"^[\w.]+:[\w.]+$", "entry point must be 'module:function'")
        module_name, _, attr = target.partition(":")
        module = importlib.import_module(module_name)
        func = getattr(module, attr, None)
        self.assertTrue(callable(func), f"{target} does not resolve to a callable")
        # main(argv) — a console script calls it with no arguments and it must tolerate that.
        params = inspect.signature(func).parameters
        self.assertLessEqual(
            len([p for p in params.values() if p.default is inspect.Parameter.empty
                 and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]),
            1,
            "the entry point must be callable as feature-pipeline() with no arguments",
        )


class RuntimeDependencyPolicy(unittest.TestCase):
    """docs/adr/003 — the runtime import graph stays standard-library only (denial path)."""

    def setUp(self) -> None:
        self.project = _load()["project"]

    def test_no_runtime_dependencies(self) -> None:
        self.assertEqual(
            self.project.get("dependencies", []), [],
            "runtime dependencies must stay empty; dev tooling goes in optional-dependencies.dev",
        )

    def test_dev_extra_carries_the_quality_tooling(self) -> None:
        dev = " ".join(self.project.get("optional-dependencies", {}).get("dev", [])).lower()
        for tool in ("ruff", "mypy", "coverage"):
            self.assertIn(tool, dev, f"optional-dependencies.dev must pin {tool}")


class RuffBaseline(unittest.TestCase):
    """AC-2 — the Ruff baseline is explicit and still fails new violations."""

    def setUp(self) -> None:
        self.ruff = _load().get("tool", {}).get("ruff", {})

    def test_target_version_and_exclusions_are_explicit(self) -> None:
        self.assertEqual(self.ruff.get("target-version"), "py311")
        excludes = " ".join(self.ruff.get("extend-exclude", []) + self.ruff.get("exclude", []))
        self.assertIn("fixtures", excludes, "generated fixtures must be excluded")
        self.assertIn("goldens", excludes, "reviewed golden fixtures must be excluded")

    def test_rule_selection_is_a_real_set_not_the_bare_default(self) -> None:
        select = set(self.ruff.get("lint", {}).get("select", []))
        self.assertTrue({"E", "F", "I"}.issubset(select), f"select is too narrow: {select}")

    def test_baseline_is_named_debt_not_a_blanket_ignore(self) -> None:
        lint = self.ruff.get("lint", {})
        ignore = lint.get("ignore", [])
        per_file = lint.get("per-file-ignores", {})
        self.assertTrue(ignore or per_file, "the pre-existing Ruff debt must be listed explicitly")
        # A blanket 'ignore everything' (e.g. "ALL" or "E", "F") would defeat the ratchet.
        self.assertNotIn("ALL", ignore)
        self.assertFalse({"E", "F"} & set(ignore), "core E/F rules must not be globally ignored")


class MypyBaseline(unittest.TestCase):
    """AC-2 — mypy has an explicit baseline; new domain modules are strict from day one."""

    def setUp(self) -> None:
        self.mypy = _load().get("tool", {}).get("mypy", {})

    def test_python_version_pinned(self) -> None:
        self.assertEqual(str(self.mypy.get("python_version")), "3.11")

    def test_checked_paths_are_declared(self) -> None:
        files = " ".join(self.mypy.get("files", []))
        self.assertIn("pipeline_core", files)
        self.assertIn("schemas", files)

    def test_known_debt_modules_are_parked_by_name(self) -> None:
        overrides = self.mypy.get("overrides", [])
        parked: set[str] = set()
        for override in overrides:
            if override.get("ignore_errors") is True:
                mods = override.get("module")
                parked.update([mods] if isinstance(mods, str) else mods or [])
        missing = _KNOWN_TYPING_DEBT - parked
        self.assertFalse(missing, f"these debt modules are not parked in a named override: {missing}")
        # And the park list must not silently swallow future modules.
        self.assertNotIn("pipeline_core.*", parked)
        self.assertNotIn("schemas.*", parked)

    def test_new_domain_modules_are_strict(self) -> None:
        overrides = self.mypy.get("overrides", [])
        for override in overrides:
            mods = override.get("module")
            names = [mods] if isinstance(mods, str) else (mods or [])
            if any(name.startswith("feature_pipeline.domain") for name in names):
                self.assertTrue(
                    override.get("disallow_untyped_defs") or override.get("strict"),
                    "feature_pipeline.domain.* must be strict from introduction",
                )
                return
        self.fail("no strict override for the not-yet-created feature_pipeline.domain.* modules")


class TestAndCoverageConfig(unittest.TestCase):
    """AC-2 / prompt L631 — unittest discovery and a ratcheted coverage floor are configured."""

    def setUp(self) -> None:
        self.coverage = _load().get("tool", {}).get("coverage", {})

    def test_discovery_command_is_recorded(self) -> None:
        run = self.coverage.get("run", {})
        self.assertTrue(run.get("branch"), "coverage must measure branches (denial paths)")
        self.assertIn("unittest discover", str(run.get("command_line", "")))
        omit = " ".join(run.get("omit", []))
        self.assertIn("fixtures", omit)
        self.assertIn("tests", omit)

    def test_coverage_floor_is_explicit(self) -> None:
        report = self.coverage.get("report", {})
        self.assertIsInstance(
            report.get("fail_under"), (int, float),
            "a ratcheted coverage floor (report.fail_under) must be set explicitly",
        )


if __name__ == "__main__":
    unittest.main()
