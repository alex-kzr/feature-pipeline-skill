"""Tests for the stage 14–16 release tail: config loader and the three stage computations."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from pipeline_core.release import (
    ReleasePolicy,
    final_diff_result,
    final_verification_result,
    load_release_policy,
    release_plan_result,
)
from pipeline_core.state import EXIT_NOT_FOUND
from schemas import SchemaError

REPO_ROOT = Path(__file__).resolve().parents[2]

_CHECKS = {
    "schema_version": 1,
    "checks": [
        {"stack": "python", "name": "core-unittests", "cwd": "feature-pipeline-skill",
         "argv": ["uv", "run", "python", "-m", "unittest", "discover", "-s", "tests", "-t", "."]},
        {"stack": "repo", "name": "whitespace-check", "cwd": ".",
         "argv": ["git", "diff", "--check"]},
    ],
}

_RELEASE = {
    "schema_version": 1,
    "final_verification": ["core-unittests", "whitespace-check"],
    "release": {
        "stage_paths": ["feature-pipeline-skill", "docs"],
        "submodule_pointer": "feature-pipeline-skill",
        "commit_message_template": "chore(feature-pipeline): {feature}",
    },
}


def _config_dir(release: dict | None = None, checks: dict | None = None) -> Path:
    directory = Path(tempfile.mkdtemp())
    (directory / "checks.json").write_text(
        json.dumps(_CHECKS if checks is None else checks), encoding="utf-8")
    (directory / "release.json").write_text(
        json.dumps(_RELEASE if release is None else release), encoding="utf-8")
    return directory


def _policy() -> ReleasePolicy:
    return load_release_policy(_config_dir() / "release.json")


class LoaderTests(unittest.TestCase):
    def test_loads_a_valid_policy_resolving_names_against_checks_json(self) -> None:
        policy = _policy()
        self.assertEqual(len(policy.final_verification), 2)
        self.assertEqual(policy.final_verification[0].cwd, "feature-pipeline-skill")
        self.assertEqual(policy.final_verification[1].argv, ("git", "diff", "--check"))
        self.assertEqual(policy.stage_paths, ("feature-pipeline-skill", "docs"))
        self.assertEqual(policy.submodule_pointer, "feature-pipeline-skill")

    def test_unknown_schema_version_is_refused(self) -> None:
        payload = json.loads(json.dumps(_RELEASE))
        payload["schema_version"] = 9
        with self.assertRaisesRegex(SchemaError, "schema_version"):
            load_release_policy(_config_dir(release=payload) / "release.json")

    def test_empty_final_verification_is_refused(self) -> None:
        payload = json.loads(json.dumps(_RELEASE))
        payload["final_verification"] = []
        with self.assertRaisesRegex(SchemaError, "final_verification"):
            load_release_policy(_config_dir(release=payload) / "release.json")

    def test_unknown_check_name_is_refused(self) -> None:
        payload = json.loads(json.dumps(_RELEASE))
        payload["final_verification"] = ["core-unittests", "does-not-exist"]
        with self.assertRaisesRegex(SchemaError, "does-not-exist"):
            load_release_policy(_config_dir(release=payload) / "release.json")

    def test_a_check_that_names_a_mutating_verb_is_refused(self) -> None:
        checks = json.loads(json.dumps(_CHECKS))
        checks["checks"].append(
            {"stack": "repo", "name": "bad", "cwd": ".", "argv": ["git", "push"]})
        payload = json.loads(json.dumps(_RELEASE))
        payload["final_verification"] = ["bad"]
        with self.assertRaisesRegex(SchemaError, "mutating verb"):
            load_release_policy(
                _config_dir(release=payload, checks=checks) / "release.json")

    def test_empty_stage_paths_is_refused(self) -> None:
        payload = json.loads(json.dumps(_RELEASE))
        payload["release"]["stage_paths"] = []
        with self.assertRaisesRegex(SchemaError, "stage_paths"):
            load_release_policy(_config_dir(release=payload) / "release.json")

    def test_absolute_stage_path_is_refused(self) -> None:
        payload = json.loads(json.dumps(_RELEASE))
        payload["release"]["stage_paths"] = ["/etc"]
        with self.assertRaises(SchemaError):
            load_release_policy(_config_dir(release=payload) / "release.json")

    def test_multiline_commit_message_template_is_refused(self) -> None:
        payload = json.loads(json.dumps(_RELEASE))
        payload["release"]["commit_message_template"] = "line one\nline two"
        with self.assertRaisesRegex(SchemaError, "single line"):
            load_release_policy(_config_dir(release=payload) / "release.json")

    def test_missing_sibling_checks_json_is_refused(self) -> None:
        directory = Path(tempfile.mkdtemp())
        (directory / "release.json").write_text(json.dumps(_RELEASE), encoding="utf-8")
        with self.assertRaisesRegex(SchemaError, "checks.json"):
            load_release_policy(directory / "release.json")

    def test_this_repositorys_release_policy_is_accepted(self) -> None:
        real = REPO_ROOT / "tools" / "feature-pipeline" / "config" / "release.json"
        if not real.is_file():
            self.skipTest("the parent repository's release policy is not in this checkout")
        policy = load_release_policy(real)
        self.assertTrue(policy.final_verification)
        self.assertTrue(policy.stage_paths)
        for command in policy.final_verification:
            self.assertNotIn("push", [t.lower() for t in command.argv])
            self.assertNotIn("commit", [t.lower() for t in command.argv])


class FinalVerificationStageTests(unittest.TestCase):
    def test_all_commands_zero_is_pass_and_records_each_argv(self) -> None:
        calls = []

        def runner(run, stage, cwd, argv, timeout=None):
            calls.append((cwd, tuple(argv)))
            return {"exit_code": 0}

        verdict, reasons, evidence = final_verification_result(
            object(), _policy(), command_runner=runner)
        self.assertEqual(verdict, "PASS")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(evidence["commands"]), 2)
        self.assertEqual(evidence["commands"][0]["cwd"], "feature-pipeline-skill")

    def test_a_nonzero_exit_is_fail(self) -> None:
        verdict, reasons, _ = final_verification_result(
            object(), _policy(),
            command_runner=lambda *a, **k: {"exit_code": 1})
        self.assertEqual(verdict, "FAIL")
        self.assertTrue(reasons)

    def test_an_unlaunchable_command_is_blocked_not_substituted(self) -> None:
        verdict, reasons, _ = final_verification_result(
            object(), _policy(),
            command_runner=lambda *a, **k: {"exit_code": EXIT_NOT_FOUND})
        self.assertEqual(verdict, "BLOCKED")
        self.assertIn("could not be launched", reasons[0])

    def test_no_declared_commands_is_blocked(self) -> None:
        empty = ReleasePolicy((), ("docs",), None, "msg")
        verdict, _, _ = final_verification_result(object(), empty)
        self.assertEqual(verdict, "BLOCKED")


class FinalDiffStageTests(unittest.TestCase):
    def test_missing_approval_is_blocked_but_records_the_digest(self) -> None:
        verdict, reasons, evidence = final_diff_result(
            approved=False, diff_probe=lambda: "diff --git a b\n")
        self.assertEqual(verdict, "BLOCKED")
        self.assertIn("pending", reasons[0])
        self.assertTrue(evidence["diff_digest"].startswith("sha256:"))

    def test_explicit_approval_is_pass(self) -> None:
        verdict, reasons, _ = final_diff_result(approved=True, diff_probe=lambda: "x")
        self.assertEqual(verdict, "PASS")
        self.assertEqual(reasons, ())


class ReleasePlanStageTests(unittest.TestCase):
    def test_without_commit_control_the_plan_is_a_dry_run(self) -> None:
        # AC-2
        verdict, _, plan = release_plan_result(
            _policy(), final_verification_pass=True, final_diff_approved=True,
            commit_control=False)
        self.assertEqual(verdict, "PASS")
        self.assertEqual(plan["mode"], "dry-run")
        self.assertFalse(plan["would_commit"])
        self.assertIn("no explicit commit control was given", plan["reasons_release_is_dry_run"])

    def test_without_final_diff_approval_the_plan_is_a_dry_run(self) -> None:
        # AC-2
        _, _, plan = release_plan_result(
            _policy(), final_verification_pass=True, final_diff_approved=False,
            commit_control=True)
        self.assertEqual(plan["mode"], "dry-run")
        self.assertFalse(plan["would_commit"])

    def test_all_controls_present_authorizes_but_still_executes_nothing(self) -> None:
        # AC-3: even fully authorized, the plan names no commit/push command and runs nothing.
        _, _, plan = release_plan_result(
            _policy(), final_verification_pass=True, final_diff_approved=True,
            commit_control=True, submodule_pointer_probe=lambda: "abc123")
        self.assertTrue(plan["would_commit"])
        self.assertEqual(plan["mode"], "commit-authorized")
        self.assertEqual(plan["submodule_pointer_target"], "abc123")
        self.assertIn("no commit code path", plan["commit"])
        self.assertIn("never", plan["push"])
        blob = json.dumps(plan).lower()
        self.assertNotIn("git push", blob)
        self.assertNotIn("git commit", blob)


class PushRefusalTests(unittest.TestCase):
    def test_the_cli_still_refuses_push_before_anything_is_resolved(self) -> None:
        # AC-3: push is refused outright.
        from pipeline_core.runner_cli import EXIT_PUSH_DENIED, main
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            code = main(["--push"])
        self.assertEqual(code, EXIT_PUSH_DENIED)
        self.assertIn("push is denied", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
