"""Regression tests for the one project-profile boundary (:mod:`pipeline_core.project_profile`).

A repository configured by ``feature-pipeline-project-setup`` carries a generated
``schema_version`` project profile plus a sibling ``checks.json``. These tests prove:

* that generated shape loads through the runner CLI and reaches plan parsing
  (rather than failing on profile schema with exit ``30``);
* the same conversion applied to *this repository's* real generated profile, when
  it is reachable from the checkout;
* a native portable-core fixture profile still loads unchanged;
* every fail-closed edge — unknown ``schema_version``, a document carrying both
  discriminators, and a half-converted document missing a required key.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline_core import runner_cli
from pipeline_core.profiles import Anchors, resolve_route
from pipeline_core.project_profile import load_runnable_profile
from schemas import SchemaError

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
FIXTURES = ROOT / "fixtures"


def _generated_profile() -> dict:
    """A generated project profile with the same shape setup_project.py writes."""
    return {
        "schema_version": 1,
        "project": "sample-repo",
        "anchors": {"agents_root": ".agents", "core_root": "feature-pipeline-skill"},
        "task_routing": [
            {"task_type": "python", "working_root": "feature-pipeline-skill"},
            {"task_type": "docs", "working_root": "docs"},
        ],
        "run_state_path": ".pipeline/runs",
        "roles": [
            {"role": "executor", "min_grants": ["read", "write"]},
            {"role": "task_verifier", "min_grants": ["read"]},
            {"role": "test_verifier", "min_grants": ["read", "run_checks"]},
        ],
    }


def _generated_checks() -> dict:
    return {
        "schema_version": 1,
        "checks": [
            {
                "stack": "python",
                "name": "core-unittests",
                "argv": ["uv", "run", "python", "-m", "unittest"],
                "cwd": "feature-pipeline-skill",
            },
            {"stack": "repo", "name": "whitespace-check", "argv": ["git", "diff", "--check"], "cwd": "."},
        ],
    }


def _seed_project(dest: Path, *, profile: dict, checks: dict | None) -> list[str]:
    """Write the generated config + a plan under ``dest`` and return CLI anchor args."""
    config = dest / "tools" / "feature-pipeline" / "config"
    config.mkdir(parents=True)
    (config / "pipeline.profile.json").write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    if checks is not None:
        (config / "checks.json").write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
    (dest / "plan.json").write_text(
        json.dumps({"feature": "sample-feature", "tasks": [{"id": "T-01", "type": "python"}]}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    agents_root = dest / "_agents"
    agents_root.mkdir()
    return [
        "--project-root", str(dest),
        "--agents-root", str(agents_root),
        "--core-root", str(dest),
    ]


def _run(args: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = runner_cli.main(args)
    return code, out.getvalue(), err.getvalue()


class GeneratedProfileIsRunnableTests(unittest.TestCase):
    def test_plan_only_dry_run_reaches_plan_parsing(self) -> None:
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            anchors = _seed_project(dest, profile=_generated_profile(), checks=_generated_checks())
            code, out, err = _run(
                anchors
                + [
                    "--profile", "tools/feature-pipeline/config/pipeline.profile.json",
                    "--plan", "plan.json",
                    "--mode", "plan-only", "--dry-run",
                ]
            )
        self.assertEqual(code, 10, err)
        self.assertIn("C1. Selected tasks:", out)
        self.assertIn("T-01: stack=python", out)
        self.assertNotIn("profile is invalid", err)

    def test_conversion_builds_a_resolvable_registry_route(self) -> None:
        profile = load_runnable_profile_from(_generated_profile(), _generated_checks())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            route = resolve_route(profile, "python", Anchors(root, root, root))
        self.assertEqual(route.route.root, "python")
        self.assertEqual(route.route.checks, ("core-unittests", "whitespace-check"))
        self.assertEqual(profile.name, "sample-repo")

    def test_this_repositorys_generated_profile_is_accepted(self) -> None:
        real = REPO_ROOT / "tools" / "feature-pipeline" / "config" / "pipeline.profile.json"
        if not real.is_file():
            self.skipTest("the parent repository's generated profile is not in this checkout")
        profile = load_runnable_profile(real)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            route = resolve_route(profile, "python", Anchors(root, root, root))
        self.assertTrue(route.route.checks, "the real generated profile resolved no checks")


class NativeProfileStillLoadsTests(unittest.TestCase):
    def test_portable_fixture_profile_loads_unchanged(self) -> None:
        profile = load_runnable_profile(FIXTURES / "shared-project" / "profile.json")
        self.assertEqual(profile.name, "shared-project")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            route = resolve_route(profile, "docs", Anchors(root, root, root))
        self.assertEqual(route.route.stack, "text")


class FailClosedTests(unittest.TestCase):
    def _write(self, directory: str, profile: dict, checks: dict | None = None) -> Path:
        path = Path(directory) / "pipeline.profile.json"
        path.write_text(json.dumps(profile), encoding="utf-8")
        if checks is not None:
            (path.parent / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
        return path

    def test_unknown_schema_version_is_refused(self) -> None:
        bad = _generated_profile()
        bad["schema_version"] = 2
        with TemporaryDirectory() as directory:
            path = self._write(directory, bad, _generated_checks())
            with self.assertRaisesRegex(SchemaError, "schema_version"):
                load_runnable_profile(path)

    def test_unknown_schema_version_through_cli_is_exit_30(self) -> None:
        bad = _generated_profile()
        bad["schema_version"] = 99
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "project"
            anchors = _seed_project(dest, profile=bad, checks=_generated_checks())
            code, _, err = _run(
                anchors
                + [
                    "--profile", "tools/feature-pipeline/config/pipeline.profile.json",
                    "--plan", "plan.json", "--mode", "plan-only", "--dry-run",
                ]
            )
        self.assertEqual(code, 30)
        self.assertIn("profile is invalid", err)

    def test_document_with_both_discriminators_is_ambiguous(self) -> None:
        both = _generated_profile()
        both["version"] = 1
        with TemporaryDirectory() as directory:
            path = self._write(directory, both, _generated_checks())
            with self.assertRaisesRegex(SchemaError, "both 'version' and 'schema_version'"):
                load_runnable_profile(path)

    def test_half_converted_document_missing_a_required_key_is_refused(self) -> None:
        partial = _generated_profile()
        del partial["task_routing"]
        with TemporaryDirectory() as directory:
            path = self._write(directory, partial, _generated_checks())
            with self.assertRaisesRegex(SchemaError, "task_routing"):
                load_runnable_profile(path)

    def test_schema_version_document_with_native_key_is_refused(self) -> None:
        mixed = _generated_profile()
        mixed["stages"] = [{"name": "implement", "subagents": ["executor"], "argv": ["run"]}]
        with TemporaryDirectory() as directory:
            path = self._write(directory, mixed, _generated_checks())
            with self.assertRaisesRegex(SchemaError, "native-core key"):
                load_runnable_profile(path)

    def test_generated_profile_without_checks_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            path = self._write(directory, _generated_profile(), checks=None)
            with self.assertRaisesRegex(SchemaError, "at least one check"):
                load_runnable_profile(path)

    def test_document_with_neither_discriminator_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            path = self._write(directory, {"name": "x", "logical_paths": {}})
            with self.assertRaisesRegex(SchemaError, "neither"):
                load_runnable_profile(path)


def load_runnable_profile_from(profile: dict, checks: dict) -> "object":
    """Round-trip ``profile`` + ``checks`` through the on-disk loader."""
    with TemporaryDirectory() as directory:
        path = Path(directory) / "pipeline.profile.json"
        path.write_text(json.dumps(profile), encoding="utf-8")
        (path.parent / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
        return load_runnable_profile(path)


if __name__ == "__main__":
    unittest.main()
