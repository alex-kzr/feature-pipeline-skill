"""DF-01 — typed domain vocabulary, paths, globs, argv, and errors.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 2),
``docs/plans/tasks/DF-01_typed-vocabulary-paths-errors.md``, ``docs/adr/002``.

Two things are pinned here:

* **AC-1** — invalid identifiers, paths, globs, and argv fail at construction, table-driven,
  each raising a typed :class:`feature_pipeline.domain.errors.DomainError` subclass with its
  stable ``code``.
* **Parity** — the new value objects accept and reject *exactly* what the live
  ``pipeline_core`` / ``feature_pipeline.contracts`` code does today, and every enum member
  value equals the string literal the current code compares against. DF-02 can then migrate a
  call site without changing which inputs are accepted.

Standard library only.
"""

from __future__ import annotations

import unittest

from feature_pipeline.contracts import (
    RUN_STATUSES,
    SHELL_TOKENS as CONTRACT_SHELL_TOKENS,
    SUBAGENTS,
    TASK_ID_RE,
    VERDICTS,
    SchemaError,
    validate_relative_path,
)
from feature_pipeline.domain import (
    Actor,
    AdapterName,
    Argv,
    Disposition,
    DomainError,
    FeatureId,
    InvalidArgv,
    InvalidGlob,
    InvalidIdentifier,
    InvalidPath,
    InvalidTerm,
    PathNotContained,
    RelativeGlob,
    RelativePath,
    RunStatus,
    StageId,
    Subagent,
    TaskId,
    TaskStatus,
    Verdict,
    ensure_within,
)

from pipeline_core import adapter_resolution, commands, state
from pipeline_core.runner_cli import _FEATURE_RE


# --------------------------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------------------------
VALID_TASK_IDS = ["DF-01", "EDF-01", "LT-9", "PCC-05", "AB-1234"]
INVALID_TASK_IDS = ["", "df-01 ", "D-01", "ABCDEFG-01", "DF-", "DF-01234", "DF01", "1F-01",
                    "DF-01x", " DF-01"]

VALID_FEATURE_IDS = ["feature-pipeline-refactor", "run_1", "v2.3", "A", "a.b_c-d"]
INVALID_FEATURE_IDS = ["", "has space", "slash/name", "back\\slash", "tab\tname", "name!"]


class IdentifierConstructionTests(unittest.TestCase):
    def test_valid_task_ids_round_trip(self) -> None:
        for raw in VALID_TASK_IDS:
            with self.subTest(raw=raw):
                self.assertEqual(str(TaskId(raw)), raw)

    def test_invalid_task_ids_fail_at_construction(self) -> None:
        for raw in INVALID_TASK_IDS:
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidIdentifier) as caught:
                    TaskId(raw)
                self.assertEqual(caught.exception.code, "invalid-identifier")

    def test_task_id_matches_contract_regex_exactly(self) -> None:
        for raw in VALID_TASK_IDS + INVALID_TASK_IDS:
            with self.subTest(raw=raw):
                accepted_by_domain = True
                try:
                    TaskId(raw)
                except InvalidIdentifier:
                    accepted_by_domain = False
                self.assertEqual(accepted_by_domain, bool(TASK_ID_RE.match(raw)))

    def test_valid_feature_ids_round_trip(self) -> None:
        for raw in VALID_FEATURE_IDS:
            with self.subTest(raw=raw):
                self.assertEqual(str(FeatureId(raw)), raw)

    def test_invalid_feature_ids_fail_with_runner_cli_message(self) -> None:
        for raw in INVALID_FEATURE_IDS:
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidIdentifier) as caught:
                    FeatureId(raw)
                self.assertEqual(
                    str(caught.exception),
                    "feature name must be a single [A-Za-z0-9._-] token",
                )

    def test_feature_id_matches_runner_cli_regex_exactly(self) -> None:
        for raw in VALID_FEATURE_IDS + INVALID_FEATURE_IDS:
            with self.subTest(raw=raw):
                accepted_by_domain = True
                try:
                    FeatureId(raw)
                except InvalidIdentifier:
                    accepted_by_domain = False
                self.assertEqual(accepted_by_domain, bool(_FEATURE_RE.match(raw)))

    def test_identifiers_are_frozen_and_hashable(self) -> None:
        self.assertEqual({TaskId("DF-01"), TaskId("DF-01")}, {TaskId("DF-01")})
        with self.assertRaises(Exception):
            TaskId("DF-01").value = "DF-02"  # type: ignore[misc]


# --------------------------------------------------------------------------------------------
# Relative paths and globs
# --------------------------------------------------------------------------------------------
# (raw input, normalized form) accepted by contracts.validate_relative_path AND RelativePath.
PATH_NORMALIZATIONS = [
    ("docs/plans/tasks/DF-01.md", "docs/plans/tasks/DF-01.md"),
    ("./docs/./x.md", "docs/x.md"),
    ("a//b/./c", "a/b/c"),
    ("src\\feature_pipeline\\domain", "src/feature_pipeline/domain"),
    (".", "."),
    ("./", "."),
]

# Rejected by BOTH contracts.validate_relative_path and RelativePath.parse.
REJECTED_PATHS = ["", "/etc/passwd", "~/secrets", "C:/Windows", "../escape", "a/../b",
                  "docs/../../x"]


class RelativePathConstructionTests(unittest.TestCase):
    def test_normalization_matches_contract_helper(self) -> None:
        for raw, expected in PATH_NORMALIZATIONS:
            with self.subTest(raw=raw):
                self.assertEqual(RelativePath.parse(raw).value, expected)
                self.assertEqual(validate_relative_path(raw, "p"), expected)

    def test_rejected_paths_fail_at_construction_both_ways(self) -> None:
        for raw in REJECTED_PATHS:
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidPath) as caught:
                    RelativePath.parse(raw)
                self.assertEqual(caught.exception.code, "invalid-path")
                with self.assertRaises(SchemaError):
                    validate_relative_path(raw, "p")

    def test_relative_path_rejects_a_glob(self) -> None:
        with self.assertRaises(InvalidPath):
            RelativePath.parse("src/**/x.py")

    def test_glob_accepts_star_but_still_rejects_traversal(self) -> None:
        self.assertEqual(
            RelativeGlob.parse("src/feature_pipeline/domain/**").value,
            "src/feature_pipeline/domain/**",
        )
        self.assertTrue(RelativeGlob.parse("src/**").is_glob)
        self.assertFalse(RelativeGlob.parse("src/x.py").is_glob)
        for raw in REJECTED_PATHS:
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidGlob):
                    RelativeGlob.parse(raw)

    def test_glob_normalization_matches_contract_helper(self) -> None:
        # contracts._spec_paths(allow_glob=True) is just validate_relative_path.
        for raw in ["src/**", "a/*.py", "docs/**/*.md"]:
            with self.subTest(raw=raw):
                self.assertEqual(
                    RelativeGlob.parse(raw).value, validate_relative_path(raw, "p")
                )


class ContainmentTests(unittest.TestCase):
    def test_within_and_relative_to(self) -> None:
        anchor = RelativePath.parse("src/feature_pipeline/domain")
        inside = RelativePath.parse("src/feature_pipeline/domain/errors.py")
        outside = RelativePath.parse("src/feature_pipeline/contracts.py")
        self.assertTrue(inside.within(anchor))
        self.assertFalse(outside.within(anchor))
        self.assertEqual(inside.relative_to(anchor).value, "errors.py")
        self.assertEqual(ensure_within(anchor, inside), inside)

    def test_repo_root_anchor_contains_everything(self) -> None:
        root = RelativePath.parse(".")
        self.assertTrue(RelativePath.parse("anything/at/all").within(root))

    def test_escape_raises_path_not_contained(self) -> None:
        anchor = RelativePath.parse("src/feature_pipeline/domain")
        outside = RelativePath.parse("tests/test_x.py")
        with self.assertRaises(PathNotContained) as caught:
            ensure_within(anchor, outside)
        self.assertEqual(caught.exception.code, "path-not-contained")
        with self.assertRaises(PathNotContained):
            outside.relative_to(anchor)


# --------------------------------------------------------------------------------------------
# Argv
# --------------------------------------------------------------------------------------------
VALID_ARGVS = [
    ["git", "diff", "--check"],
    ["uv", "run", "python", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
    ("python", "-c", "print(1)"),
]
REJECTED_ARGVS = [
    [],
    "git diff --check",                       # bare string hides a missing split
    b"git",                                   # bytes
    {"argv": ["git"]},                        # mapping
    ["git", ""],                              # empty token
    ["git", "status", "&&", "git", "push"],   # shell operator token
    ["sh", "-c", "a | b"],                    # embedded pipe
    ["echo", "x", ">", "f"],
    ["a", "b;c"],
    ["a", 3],                                 # non-string token
]


class ArgvConstructionTests(unittest.TestCase):
    def test_valid_argvs_round_trip(self) -> None:
        for raw in VALID_ARGVS:
            with self.subTest(raw=raw):
                self.assertEqual(tuple(Argv.parse(raw)), tuple(raw))
                self.assertEqual(len(Argv.parse(raw)), len(raw))

    def test_rejected_argvs_fail_at_construction(self) -> None:
        for raw in REJECTED_ARGVS:
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidArgv) as caught:
                    Argv.parse(raw)
                self.assertEqual(caught.exception.code, "invalid-argv")

    def test_shell_token_set_matches_contract(self) -> None:
        from feature_pipeline.domain.argv import SHELL_TOKENS as DOMAIN_SHELL_TOKENS

        self.assertEqual(DOMAIN_SHELL_TOKENS, CONTRACT_SHELL_TOKENS)

    def test_shell_operator_message_names_the_offending_token(self) -> None:
        with self.assertRaises(InvalidArgv) as caught:
            Argv.parse(["git", "a&&b"])
        self.assertIn("a&&b", str(caught.exception))
        self.assertIn("its own recorded exit code", str(caught.exception))


# --------------------------------------------------------------------------------------------
# Closed vocabularies
# --------------------------------------------------------------------------------------------
class VocabularyParityTests(unittest.TestCase):
    def test_task_status_covers_state_machine(self) -> None:
        names = set(state.TASK_TRANSITIONS)
        for successors in state.TASK_TRANSITIONS.values():
            names |= set(successors)
        self.assertEqual({s.value for s in TaskStatus}, names)

    def test_run_status_matches_contract(self) -> None:
        self.assertEqual({s.value for s in RunStatus}, set(RUN_STATUSES))

    def test_verdict_and_disposition_match_live_constants(self) -> None:
        self.assertEqual({v.value for v in Verdict}, set(VERDICTS))
        self.assertEqual(
            {d.value for d in Disposition},
            {commands.DISPOSITION_PASS, commands.DISPOSITION_FAIL, commands.DISPOSITION_BLOCKED},
        )

    def test_actor_matches_state_constants(self) -> None:
        self.assertEqual(
            {a.value for a in Actor},
            {state.ACTOR_RUNNER, state.ACTOR_EXECUTOR, state.ACTOR_HUMAN},
        )

    def test_subagent_matches_contract(self) -> None:
        self.assertEqual({s.value for s in Subagent}, set(SUBAGENTS))

    def test_adapter_name_matches_known_adapters(self) -> None:
        self.assertEqual(
            tuple(a.value for a in AdapterName), adapter_resolution.KNOWN_ADAPTERS
        )

    def test_stage_id_numbers_are_dense_and_ordered(self) -> None:
        numbers = [s.number for s in StageId]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_post_task_stage_names_and_numbers_match_code(self) -> None:
        from pipeline_core import post_task

        for number, name in post_task.POST_TASK_STAGES:
            with self.subTest(stage=name):
                self.assertEqual(StageId(name).number, number)

    def test_parse_is_fail_closed_and_idempotent(self) -> None:
        self.assertIs(TaskStatus.parse("verified"), TaskStatus.VERIFIED)
        self.assertIs(TaskStatus.parse(TaskStatus.BLOCKED), TaskStatus.BLOCKED)
        for bad in ["", "done", "VERIFIED", None, 3]:
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidTerm) as caught:
                    Verdict.parse(bad)
                self.assertEqual(caught.exception.code, "invalid-term")

    def test_str_enum_values_compare_equal_to_plain_strings(self) -> None:
        self.assertEqual(TaskStatus.VERIFIED, "verified")
        self.assertEqual(Verdict.PASS, "PASS")
        self.assertEqual(f"{Actor.RUNNER}", "runner")


class DomainErrorTests(unittest.TestCase):
    def test_every_subclass_is_a_domain_error_and_a_value_error(self) -> None:
        for exc in (InvalidIdentifier, InvalidPath, InvalidGlob, PathNotContained,
                    InvalidArgv, InvalidTerm):
            with self.subTest(exc=exc):
                self.assertTrue(issubclass(exc, DomainError))
                self.assertTrue(issubclass(exc, ValueError))

    def test_codes_are_stable_and_distinct(self) -> None:
        codes = [InvalidIdentifier.code, InvalidPath.code, InvalidGlob.code,
                 PathNotContained.code, InvalidArgv.code, InvalidTerm.code]
        self.assertEqual(len(codes), len(set(codes)))


if __name__ == "__main__":
    unittest.main()
