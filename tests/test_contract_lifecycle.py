"""Contract-test lifecycle invariants."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests import test_critical_behavior_characterization  # noqa: F401
from tests._umbrella import require_umbrella, umbrella_root
from tests.contract_lifecycle import superseded_characterizations, validate_registry


class SupersededCharacterizationTests(unittest.TestCase):
    def test_every_superseded_test_has_an_adr_and_live_replacement(self) -> None:
        records = superseded_characterizations()

        self.assertTrue(records)
        # ``validate_registry`` resolves each superseded entry's ADR under ``docs/adr/``,
        # which ships only in the umbrella working tree; on a standalone
        # feature-pipeline-skill checkout the ADR files are structurally absent.
        require_umbrella("docs/adr/*.md")
        self.assertEqual(validate_registry(umbrella_root()), ())
        self.assertTrue(all(record.adr and record.replacement for record in records))
