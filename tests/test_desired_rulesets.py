"""UGA-13 - the checked-in desired-ruleset snapshot must equal a fresh render.

``ci/desired_rulesets.json`` is a reviewed, versioned snapshot of the exact managed branch
rulesets an operator applies in UGA-14. It is rendered from the UGA-10 identity contract
(:func:`ci.promotion.required_check_contract`) through the UGA-12 pure renderer
(:func:`ci.required_checks.render_desired_ruleset`) plus the explicit
:class:`ci.required_checks.RulesetTarget` inputs declared in :mod:`tests.desired_rulesets`.

These tests fail on any drift between the file and a fresh render, so a contract change that
is not reflected in the snapshot is a red test rather than silent divergence.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from ci import contract
from ci import promotion
from ci import required_checks as rc

from tests import desired_rulesets as ds


class DesiredRulesetsSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.loaded = contract.load()
        self.rcc = promotion.required_check_contract(self.loaded)
        self.snapshot = json.loads(ds.SNAPSHOT_PATH.read_text(encoding="utf-8"))

    def test_snapshot_is_a_json_object(self) -> None:
        self.assertIsInstance(self.snapshot, dict)

    def test_snapshot_equals_a_fresh_render(self) -> None:
        self.assertEqual(self.snapshot, ds.render_snapshot(self.loaded))

    def test_snapshot_file_is_canonical_serialization(self) -> None:
        self.assertEqual(
            ds.SNAPSHOT_PATH.read_text(encoding="utf-8"),
            ds.serialize(ds.render_snapshot(self.loaded)),
        )

    def test_both_repositories_match_the_uga12_renderer(self) -> None:
        for target in ds.TARGETS:
            with self.subTest(role=target.role):
                self.assertEqual(
                    self.snapshot[target.role],
                    rc.render_desired_ruleset(self.rcc, target),
                )

    def test_targets_are_explicit_inputs_for_both_named_repositories(self) -> None:
        declared = {(t["repository"], t["role"]) for t in self.snapshot["targets"]}
        self.assertEqual(
            declared,
            {
                ("alex-kzr/feature-pipeline-skill", "producer"),
                ("alex-kzr/feature-pipeline", "consumer"),
            },
        )
        for entry in self.snapshot["targets"]:
            self.assertEqual(entry["default_branch"], "main")

    def test_contexts_derive_from_the_shared_api_not_literals(self) -> None:
        def contexts(role: str) -> list[str]:
            return [
                check["context"]
                for rule in self.snapshot[role]["rules"]
                if rule["type"] == "required_status_checks"
                for check in rule["parameters"]["required_status_checks"]
            ]

        self.assertEqual(contexts("producer"), list(self.rcc.producer))
        self.assertEqual(contexts("consumer"), [*self.rcc.consumer, self.rcc.promotion])

    def test_identity_contract_drift_breaks_the_snapshot(self) -> None:
        drifted = rc.RequiredCheckContract(
            version=self.rcc.version,
            producer=("renamed lint/types check", *self.rcc.producer[1:]),
            consumer=self.rcc.consumer,
            promotion=self.rcc.promotion,
        )
        producer_target = next(t for t in ds.TARGETS if t.role == "producer")
        self.assertNotEqual(
            self.snapshot["producer"],
            rc.render_desired_ruleset(drifted, producer_target),
        )

    def test_cli_reports_up_to_date_without_write(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ds.main([])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("up to date", out.getvalue())

    def test_cli_write_regenerates_file_and_plan_doc_block(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            snapshot = directory / "desired_rulesets.json"
            plan_doc = directory / "plan.md"
            plan_doc.write_text(
                "intro\n\n"
                f"{ds._DOC_BEGIN}\n\n```json\nstale\n```\n\n{ds._DOC_END}\n\ntail\n",
                encoding="utf-8",
            )
            with mock.patch.object(ds, "SNAPSHOT_PATH", snapshot), mock.patch.object(
                ds, "PLAN_DOC_PATH", plan_doc
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = ds.main(["--write"])
                self.assertEqual(code, 0)
                self.assertEqual(
                    json.loads(snapshot.read_text(encoding="utf-8")),
                    ds.render_snapshot(self.loaded),
                )
                block = json.loads(
                    plan_doc.read_text(encoding="utf-8").split("```json\n", 1)[1].split(
                        "\n```", 1
                    )[0]
                )
                self.assertEqual(block, ds.render_snapshot(self.loaded))
                # Idempotent: a second write is a no-op that still succeeds.
                self.assertEqual(ds.main(["--write"]), 0)

    def test_cli_flags_a_stale_snapshot(self) -> None:
        with TemporaryDirectory() as raw:
            snapshot = Path(raw) / "desired_rulesets.json"
            snapshot.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(ds, "SNAPSHOT_PATH", snapshot):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = ds.main([])
            self.assertEqual(code, 1)
            self.assertIn("stale", err.getvalue())

    def test_snapshot_makes_no_network_call(self) -> None:
        # render_snapshot is a pure function of the manifest; guard against a stray import
        # of the live adapter sneaking a call in.
        import urllib.request

        original = urllib.request.urlopen

        def forbidden(*_args, **_kwargs):  # pragma: no cover - only runs on regression
            raise AssertionError("render_snapshot attempted a network call")

        urllib.request.urlopen = forbidden
        try:
            ds.render_snapshot(self.loaded)
        finally:
            urllib.request.urlopen = original


if __name__ == "__main__":
    unittest.main()
