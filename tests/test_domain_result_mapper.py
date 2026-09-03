"""DF-01 — one ``ResultMapper`` owns typed-outcome -> CLI exit-code translation.

Plan: ``docs/plans/tasks/DF-01_typed-vocabulary-paths-errors.md`` (AC-2), ``docs/adr/002``.

* **AC-2** — a single mapper owns every accepted exit-code translation for the migrated
  slice: a :class:`DomainError` maps to exit ``30``, and the canonical code/meaning table is
  the mapper's, not a scatter of literals.
* **Parity** — the mapper's numbers and one-line meanings are byte-identical to
  ``pipeline_core.runner_cli``'s ``EXIT_*`` constants and ``EXIT_MEANINGS`` table, and to the
  frozen ``tests/goldens/compatibility/exit-code-table.txt`` baseline.

Standard library only.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from feature_pipeline.application.results import Outcome, PipelineResult, ResultMapper
from feature_pipeline.domain.errors import (
    DomainError,
    InvalidArgv,
    InvalidIdentifier,
    InvalidPath,
    InvalidTerm,
)

from pipeline_core import runner_cli

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "goldens" / "compatibility" / "exit-code-table.txt"


class ResultMapperExitCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapper = ResultMapper()

    def test_outcome_to_code_matches_runner_cli_constants(self) -> None:
        self.assertEqual(self.mapper.exit_code(Outcome.OK), runner_cli.EXIT_OK)
        self.assertEqual(
            self.mapper.exit_code(Outcome.GATE_PENDING), runner_cli.EXIT_GATE_PENDING
        )
        self.assertEqual(self.mapper.exit_code(Outcome.BLOCKED), runner_cli.EXIT_BLOCKED)
        self.assertEqual(self.mapper.exit_code(Outcome.ERROR), runner_cli.EXIT_ERROR)
        self.assertEqual(
            self.mapper.exit_code(Outcome.PUSH_DENIED), runner_cli.EXIT_PUSH_DENIED
        )

    def test_meanings_match_runner_cli_table(self) -> None:
        for outcome in (Outcome.OK, Outcome.GATE_PENDING, Outcome.BLOCKED, Outcome.ERROR):
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    self.mapper.meaning(outcome),
                    runner_cli.EXIT_MEANINGS[self.mapper.exit_code(outcome)],
                )

    def test_push_denied_meaning_is_the_frozen_refusal_string(self) -> None:
        self.assertEqual(
            self.mapper.meaning(Outcome.PUSH_DENIED), runner_cli.PUSH_DENIED_MESSAGE
        )

    def test_accepts_a_result_or_a_bare_outcome(self) -> None:
        result = PipelineResult(Outcome.BLOCKED, "held lease")
        self.assertEqual(self.mapper.exit_code(result), 20)
        self.assertEqual(result.exit_code, 20)
        self.assertEqual(self.mapper.exit_code(Outcome.BLOCKED), 20)

    def test_exit_codes_table_is_the_public_frozen_map(self) -> None:
        self.assertEqual(
            ResultMapper.EXIT_CODES,
            {
                Outcome.OK: 0,
                Outcome.GATE_PENDING: 10,
                Outcome.BLOCKED: 20,
                Outcome.ERROR: 30,
                Outcome.PUSH_DENIED: 1,
            },
        )


class DomainErrorMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapper = ResultMapper()

    def test_every_domain_error_maps_to_error_exit_30(self) -> None:
        for exc in (
            InvalidIdentifier("bad id"),
            InvalidPath("bad path"),
            InvalidArgv("bad argv"),
            InvalidTerm("bad term"),
            DomainError("generic", code="custom"),
        ):
            with self.subTest(exc=type(exc).__name__):
                result = self.mapper.from_domain_error(exc)
                self.assertEqual(result.outcome, Outcome.ERROR)
                self.assertEqual(result.exit_code, 30)
                self.assertEqual(result.message, str(exc))

    def test_non_domain_error_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.mapper.from_domain_error(ValueError("not a domain error"))  # type: ignore[arg-type]


class GoldenExitCodeTableTests(unittest.TestCase):
    """The mapper must be able to reproduce every row of the frozen exit-code table."""

    LABEL_TO_OUTCOME = {
        0: Outcome.OK,
        1: Outcome.PUSH_DENIED,
        10: Outcome.GATE_PENDING,
        20: Outcome.BLOCKED,
        30: Outcome.ERROR,
    }

    def test_reproduces_every_golden_row(self) -> None:
        mapper = ResultMapper()
        rows = [
            line for line in GOLDEN.read_text(encoding="utf-8").splitlines() if ":" in line
        ]
        self.assertGreaterEqual(len(rows), 8)
        for row in rows:
            scenario, code_text = (part.strip() for part in row.rsplit(":", 1))
            code = int(code_text)
            with self.subTest(scenario=scenario):
                outcome = self.LABEL_TO_OUTCOME[code]
                self.assertEqual(mapper.exit_code(outcome), code)


if __name__ == "__main__":
    unittest.main()
