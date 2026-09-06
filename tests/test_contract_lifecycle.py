"""Contract-test lifecycle invariants."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests import test_critical_behavior_characterization  # noqa: F401
from tests.contract_lifecycle import superseded_characterizations, validate_registry


class SupersededCharacterizationTests(unittest.TestCase):
    def test_every_superseded_test_has_an_adr_and_live_replacement(self) -> None:
        records = superseded_characterizations()

        self.assertTrue(records)
        self.assertEqual(validate_registry(Path(__file__).parents[2]), ())
        self.assertTrue(all(record.adr and record.replacement for record in records))
