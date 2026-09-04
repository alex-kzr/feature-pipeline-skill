"""RS-03 — the parsed read-only Git allowlist and the explicit-outcome inspector.

Covers :mod:`feature_pipeline.infrastructure.git.safety` (ADR 007 §7, BL-02 F-06),
:mod:`feature_pipeline.infrastructure.git.inspector`, and the ``pipeline_core.git_port``
cutover onto them. The BL-02 bypass argvs are used as the first red cases:
``git -c alias.x=push x``, ``git -C <dir> push``, and ``git --git-dir=… push`` must each be
denied by the allowlist, not handed to the subprocess.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from feature_pipeline.infrastructure.git import (
    GitInspection,
    GitPolicyError,
    ParsedGitCommand,
    inspect,
    parse_read_only_git,
)
from feature_pipeline.infrastructure.git.safety import (
    CODE_DISALLOWED_ARGUMENT,
    CODE_EMPTY_ARGV,
    CODE_LEADING_OPTION,
    CODE_MALFORMED_ARGUMENT,
    CODE_UNKNOWN_OPERATION,
    READ_ONLY_OPERATIONS,
)
from pipeline_core.git_port import GitPort, GitResult, GitSafetyError


class ParseAcceptsKnownReadOnlyOperations(unittest.TestCase):
    def test_the_operations_worktree_attribution_actually_runs(self) -> None:
        for argv in (
            ["rev-parse", "--is-inside-work-tree"],
            ["ls-files", "-z"],
            ["ls-files", "--others", "--exclude-standard", "-z"],
        ):
            with self.subTest(argv=argv):
                parsed = parse_read_only_git(argv)
                self.assertIsInstance(parsed, ParsedGitCommand)
                self.assertEqual(parsed.argv, tuple(argv))

    def test_diff_and_status_inspection_variants(self) -> None:
        for argv in (
            ["diff", "--stat"],
            ["diff", "--name-only", "HEAD"],
            ["diff", "--", "src/pkg/mod.py"],
            ["status", "--porcelain"],
            ["status", "--porcelain=v1", "-z"],
            ["status", "--untracked-files=no"],
            ["log", "--oneline", "--max-count=5"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(parse_read_only_git(argv).argv, tuple(argv))

    def test_parser_returns_argv_unchanged_no_rewrite_or_injection(self) -> None:
        argv = ["diff", "--stat", "--", "a b/weird name.py"]
        self.assertEqual(parse_read_only_git(argv).argv, tuple(argv))

    def test_operand_after_double_dash_is_not_reparsed_as_a_flag(self) -> None:
        # A pathspec that looks like an option is safe once ``--`` has opened operands.
        self.assertEqual(
            parse_read_only_git(["ls-files", "--", "--not-a-flag"]).argv,
            ("ls-files", "--", "--not-a-flag"),
        )


class ParseFailsClosed(unittest.TestCase):
    def _code(self, argv: list[str]) -> str:
        with self.assertRaises(GitPolicyError) as caught:
            parse_read_only_git(argv)
        return caught.exception.code

    def test_empty_argv(self) -> None:
        self.assertEqual(self._code([]), CODE_EMPTY_ARGV)

    def test_nul_byte_in_any_token(self) -> None:
        self.assertEqual(self._code(["status", "--\x00"]), CODE_MALFORMED_ARGUMENT)

    def test_bl02_bypass_c_alias_indirection(self) -> None:
        self.assertEqual(self._code(["-c", "alias.sync=push", "sync"]), CODE_LEADING_OPTION)

    def test_bl02_bypass_global_option_before_subcommand(self) -> None:
        self.assertEqual(self._code(["-C", "/somewhere/else", "push"]), CODE_LEADING_OPTION)

    def test_bl02_bypass_attached_git_dir_redirect(self) -> None:
        self.assertEqual(
            self._code(["--git-dir=../other/.git", "push"]), CODE_LEADING_OPTION
        )

    def test_paginate_and_other_leading_options_are_refused(self) -> None:
        for argv in (["-P", "status"], ["--paginate", "log"], ["--no-pager", "diff"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._code(argv), CODE_LEADING_OPTION)

    def test_plain_mutating_subcommand_is_not_on_the_allowlist(self) -> None:
        for argv in (["push"], ["reset", "--hard"], ["checkout", "main"], ["commit"],
                     ["clean", "-fdx"], ["sync"], ["frobnicate"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._code(argv), CODE_UNKNOWN_OPERATION)

    def test_unknown_flag_for_a_known_operation(self) -> None:
        for argv in (["status", "--force"], ["ls-files", "--delete-everything"],
                     ["diff", "--unknown"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._code(argv), CODE_DISALLOWED_ARGUMENT)

    def test_universally_dangerous_option_after_a_known_operation(self) -> None:
        for argv in (
            ["diff", "--output=/tmp/x", "HEAD"],
            ["diff", "-o", "/tmp/x"],
            ["log", "--exec-path=/evil"],
            ["show", "-c", "core.pager=evil"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self._code(argv), CODE_DISALLOWED_ARGUMENT)

    def test_operation_that_takes_no_operand_rejects_a_bare_token(self) -> None:
        # ``rev-parse`` allows operands; ``for-each-ref`` here is schema-driven — confirm the
        # generic "no operand" branch by using an operation configured without operands.
        no_operand = [name for name, schema in READ_ONLY_OPERATIONS.items()
                      if not schema.allows_operand]
        self.assertIn("for-each-ref", no_operand)
        self.assertEqual(self._code(["for-each-ref", "refs/heads/*"]), CODE_DISALLOWED_ARGUMENT)


class GitPortCutover(unittest.TestCase):
    def setUp(self) -> None:
        self._real_run = subprocess.run
        self._calls: list[list[str]] = []

        def _fake_run(argv, **_kwargs):  # noqa: ANN001, ANN003
            self._calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "out", "")

        subprocess.run = _fake_run  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(subprocess, "run", self._real_run))

    def test_denied_argv_raises_and_never_reaches_the_subprocess(self) -> None:
        port = GitPort(Path("."))
        for argv in (["-c", "alias.x=push", "x"], ["-C", "/x", "push"],
                     ["--git-dir=../x/.git", "push"], ["push"], ["status", "--force"]):
            with self.subTest(argv=argv):
                with self.assertRaises(GitSafetyError):
                    port.run(argv)
        self.assertEqual(self._calls, [])

    def test_allowed_argv_is_forwarded_verbatim(self) -> None:
        result = GitPort(Path(".")).run(["ls-files", "--others", "--exclude-standard", "-z"])
        self.assertIsInstance(result, GitResult)
        self.assertEqual(
            self._calls, [["git", "ls-files", "--others", "--exclude-standard", "-z"]]
        )

    def test_git_safety_error_is_still_a_value_error(self) -> None:
        self.assertTrue(issubclass(GitSafetyError, ValueError))


class InspectorReportsOutcomeExplicitly(unittest.TestCase):
    def test_policy_denial_is_unavailable_with_a_reason_not_an_empty_success(self) -> None:
        result = inspect(".", ["push"], invoker=self._never_called)
        self.assertIsInstance(result, GitInspection)
        self.assertFalse(result.available)
        self.assertTrue(result.reason)
        self.assertEqual(result.stdout, "")

    def test_nonzero_exit_is_unavailable_and_carries_the_code_and_reason(self) -> None:
        def failing(argv, cwd, timeout):  # noqa: ANN001, ANN202
            return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")

        result = inspect(".", ["rev-parse", "--is-inside-work-tree"], invoker=failing)
        self.assertFalse(result.available)
        self.assertEqual(result.returncode, 128)
        self.assertIn("128", result.reason or "")
        self.assertIn("not a git repository", result.reason or "")

    def test_launch_failure_is_unavailable_with_a_reason(self) -> None:
        def raising(argv, cwd, timeout):  # noqa: ANN001, ANN202
            raise OSError("git executable not found")

        result = inspect(".", ["status", "--porcelain"], invoker=raising)
        self.assertFalse(result.available)
        self.assertIn("could not be launched", result.reason or "")

    def test_clean_exit_zero_is_available_and_carries_stdout(self) -> None:
        def ok(argv, cwd, timeout):  # noqa: ANN001, ANN202
            return subprocess.CompletedProcess(argv, 0, " M src/pkg/mod.py\n", "")

        result = inspect(".", ["status", "--porcelain"], invoker=ok)
        self.assertTrue(result.available)
        self.assertEqual(result.stdout, " M src/pkg/mod.py\n")
        self.assertIsNone(result.reason)

    @staticmethod
    def _never_called(argv, cwd, timeout):  # noqa: ANN001, ANN205
        raise AssertionError("a denied argv must never reach the invoker")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
