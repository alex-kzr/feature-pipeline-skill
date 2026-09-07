"""UGA-13 - the remote-enforcement plan page is checked against executable data.

``docs/validation/github-actions-remote-enforcement-plan.md`` is the offline, reversible half
of the retired UGA-09. Every required status-check identity it lists, and the rendered ruleset
JSON block it embeds, are compared mechanically here to:

* the shared UGA-10 identity contract ``ci.promotion.required_check_contract``; and
* :func:`tests.desired_rulesets.render_snapshot`, itself a pure function of that contract and
  the UGA-12 renderer.

So copying an identity into prose, or letting the embedded snapshot drift from the renderer,
is a red test rather than silent divergence.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from ci import contract
from ci import promotion
from ci import required_checks as rc

from tests import desired_rulesets as ds

DOC = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "validation"
    / "github-actions-remote-enforcement-plan.md"
)


def _fenced_json_blocks(text: str) -> list[object]:
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)]


def _context_column(text: str, heading: str) -> list[str]:
    lines = text[text.index(heading):].splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("|"))
    rows: list[str] = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        rows.append(line.strip().strip("|").split("|")[0].strip().strip("`"))
    return rows


class RemoteEnforcementPlanDoc(unittest.TestCase):
    def setUp(self) -> None:
        self.text = DOC.read_text(encoding="utf-8")
        self.loaded = contract.load()
        self.rcc = promotion.required_check_contract(self.loaded)

    def test_producer_context_table_is_exactly_the_shared_contract(self) -> None:
        self.assertEqual(
            _context_column(self.text, "### Producer"), list(self.rcc.producer)
        )

    def test_consumer_context_table_is_exactly_the_shared_contract(self) -> None:
        self.assertEqual(
            _context_column(self.text, "### Consumer and promotion"),
            [*self.rcc.consumer, self.rcc.promotion],
        )

    def test_every_contract_identity_appears_verbatim_in_the_page(self) -> None:
        for identity in (*self.rcc.producer, *self.rcc.consumer, self.rcc.promotion):
            with self.subTest(identity=identity):
                self.assertIn(identity, self.text)

    def test_embedded_snapshot_block_equals_a_fresh_render(self) -> None:
        blocks = _fenced_json_blocks(self.text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0], ds.render_snapshot(self.loaded))

    def test_embedded_snapshot_block_equals_the_checked_in_file(self) -> None:
        blocks = _fenced_json_blocks(self.text)
        self.assertEqual(
            blocks[0], json.loads(ds.SNAPSHOT_PATH.read_text(encoding="utf-8"))
        )

    def test_page_states_it_performs_no_remote_change(self) -> None:
        lowered = self.text.lower()
        for token in ("no github api call", "no ruleset", "verified"):
            with self.subTest(token=token):
                self.assertIn(token, lowered)

    def test_page_names_the_acceptance_check_and_schema_keys(self) -> None:
        self.assertIn("verify_acceptance_evidence", self.text)
        self.assertIn("ci.required_checks", self.text)
        for key in rc.ACCEPTANCE_EVIDENCE_SCHEMA["top_level_keys"]:
            with self.subTest(key=key):
                self.assertIn(key, self.text)

    def test_page_declares_the_uga14_offline_cli_and_both_outcomes(self) -> None:
        self.assertIn("python -m ci.required_checks verify", self.text)
        self.assertIn("github-actions-remote-acceptance.md", self.text)
        # fixture invocation named, and the missing-live-report failure documented
        self.assertIn("acceptance-pass.json", self.text)
        self.assertIn("No such file or directory", self.text)

    def test_runbook_orders_producer_evidence_before_promotion_dispatch(self) -> None:
        lowered = self.text.lower()
        self.assertLess(lowered.index("quality-gates"), lowered.index("core-promotion"))

    def test_runbook_covers_scoped_rollback_and_concurrent_drift(self) -> None:
        lowered = self.text.lower()
        for token in (
            "pre-image",
            "delete the newly created managed ruleset",
            "concurrent",
            "readback",
            "exact",
        ):
            with self.subTest(token=token):
                self.assertIn(token, lowered)


if __name__ == "__main__":
    unittest.main()
