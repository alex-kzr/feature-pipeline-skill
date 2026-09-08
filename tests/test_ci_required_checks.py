"""UGA-12 - declarative required-check rendering and the offline evidence verifier.

``ci/required_checks.py`` is the executable spine for the remote-enforcement work:

* :func:`render_desired_ruleset` is a pure function of the UGA-10 identity contract plus an
  explicit :class:`RulesetTarget` (repository, role, default branch, managed ruleset name,
  enforcement mode, check-provider binding). It never reads workflow YAML or calls GitHub and it
  never invents repository/branch settings from the identity-only contract.
* :func:`verify_acceptance_evidence` is a pure judge over a captured evidence record. It passes
  only when every contract identity has one successful run bound to a single core SHA, with
  complete run/step evidence, the right repository/provider, separate core/umbrella SHA binding,
  and post-apply rules that match the renderer. Caller-supplied evidence can never redefine the
  required identities.
* the ``fetch_*`` live adapter mirrors ``ci/promotion.py``: read-only, no mutation or dispatch,
  reads only the *name* of a token env var, follows pagination, wraps transport errors, and
  redacts the token value. No unit test in this module performs a network call.
* ``python -m ci.required_checks verify`` is a strictly offline CLI: it reads one fenced JSON
  acceptance record from a Markdown report, loads the identity contract and explicit targets,
  and exits 0 only for PASS.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from ci import contract
from ci import required_checks as rc

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci" / "required_checks"

CORE_SHA = "1111111111111111111111111111111111111111"
UMBRELLA_SHA = "abcabcabcabcabcabcabcabcabcabcabcabcabcab"
OTHER_SHA = "2222222222222222222222222222222222222222"
PRODUCER_REPO = "example/feature-pipeline-skill"
UMBRELLA_REPO = "example/feature-pipeline"


def _contract() -> contract.Contract:
    return contract.load()


def _producer_target() -> rc.RulesetTarget:
    return rc.RulesetTarget(
        repository=PRODUCER_REPO,
        role="producer",
        default_branch="main",
        ruleset_name="core-required-checks",
        enforcement="active",
        check_provider_id=15368,
    )


def _consumer_target() -> rc.RulesetTarget:
    return rc.RulesetTarget(
        repository=UMBRELLA_REPO,
        role="consumer",
        default_branch="main",
        ruleset_name="umbrella-required-checks",
        enforcement="active",
        check_provider_id=15368,
    )


def _targets() -> tuple[rc.RulesetTarget, ...]:
    return (_producer_target(), _consumer_target())


def _load_pass_evidence() -> dict:
    return json.loads((FIXTURES / "acceptance-pass.json").read_text(encoding="utf-8"))


class RenderDesiredRuleset(unittest.TestCase):
    def test_producer_ruleset_is_a_pure_function_of_the_contract(self) -> None:
        loaded = _contract()
        first = rc.render_desired_ruleset(rc.required_check_contract(loaded), _producer_target())
        second = rc.render_desired_ruleset(
            rc.required_check_contract(loaded), _producer_target()
        )
        self.assertEqual(first, second)
        self.assertEqual(first["name"], "core-required-checks")
        self.assertEqual(first["target"], "branch")
        self.assertEqual(first["enforcement"], "active")
        self.assertEqual(
            first["conditions"]["ref_name"]["include"], ["refs/heads/main"]
        )
        contexts = [
            check["context"]
            for rule in first["rules"]
            if rule["type"] == "required_status_checks"
            for check in rule["parameters"]["required_status_checks"]
        ]
        self.assertEqual(
            contexts, list(rc.required_check_contract(loaded).producer)
        )

    def test_consumer_ruleset_carries_consumer_cells_and_promotion(self) -> None:
        loaded = _contract()
        rendered = rc.render_desired_ruleset(
            rc.required_check_contract(loaded), _consumer_target()
        )
        contexts = [
            check["context"]
            for rule in rendered["rules"]
            if rule["type"] == "required_status_checks"
            for check in rule["parameters"]["required_status_checks"]
        ]
        required = rc.required_check_contract(loaded)
        self.assertEqual(contexts, [*required.consumer, required.promotion])
        for rule in rendered["rules"]:
            if rule["type"] == "required_status_checks":
                for check in rule["parameters"]["required_status_checks"]:
                    self.assertEqual(check["integration_id"], 15368)

    def test_changing_gate_identities_changes_checks_not_repo_or_branch(self) -> None:
        loaded = _contract()
        base = rc.render_desired_ruleset(
            rc.required_check_contract(loaded), _producer_target()
        )
        drifted_contract = rc.RequiredCheckContract(
            version=1,
            producer=("renamed lint check", *rc.required_check_contract(loaded).producer[1:]),
            consumer=rc.required_check_contract(loaded).consumer,
            promotion=rc.required_check_contract(loaded).promotion,
        )
        drifted = rc.render_desired_ruleset(drifted_contract, _producer_target())
        self.assertNotEqual(base["rules"], drifted["rules"])
        self.assertEqual(base["conditions"], drifted["conditions"])
        self.assertEqual(base["name"], drifted["name"])

    def test_renderer_never_infers_repository_from_the_identity_contract(self) -> None:
        loaded = _contract()
        rendered = rc.render_desired_ruleset(
            rc.required_check_contract(loaded), _producer_target()
        )
        # The identity contract has no repository/branch data; the target supplies it.
        self.assertEqual(rendered["_repository"], PRODUCER_REPO)


class VerifyAcceptanceEvidence(unittest.TestCase):
    def test_passing_fixture_is_accepted(self) -> None:
        result = rc.verify_acceptance_evidence(
            _load_pass_evidence(), _contract(), _targets()
        )
        self.assertTrue(result.ok, result.reasons)
        self.assertEqual(result.reasons, ())

    def test_pass_fixture_covers_exactly_the_current_contract_identities(self) -> None:
        evidence = _load_pass_evidence()
        seen = {run["check_name"] for run in evidence["runs"]}
        required = rc.required_check_contract(_contract())
        self.assertEqual(
            seen, set((*required.producer, *required.consumer, required.promotion))
        )

    def test_missing_identity_fails_closed(self) -> None:
        evidence = _load_pass_evidence()
        dropped = evidence["runs"].pop()["check_name"]
        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())
        self.assertFalse(result.ok)
        self.assertTrue(any(dropped in reason for reason in result.reasons))

    def test_non_success_conclusion_fails_closed(self) -> None:
        evidence = _load_pass_evidence()
        evidence["runs"][0]["conclusion"] = "failure"
        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())
        self.assertFalse(result.ok)
        self.assertTrue(any("success" in reason for reason in result.reasons))

    def test_a_second_core_sha_fails_closed(self) -> None:
        evidence = _load_pass_evidence()
        for run in evidence["runs"]:
            if run["role"] == "producer":
                run["head_sha"] = OTHER_SHA
                break
        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())
        self.assertFalse(result.ok)
        self.assertTrue(any("SHA" in reason for reason in result.reasons))

    def test_duplicate_run_with_same_workflow_evidence_and_second_sha_fails_closed(self) -> None:
        evidence = _load_pass_evidence()
        original = next(run for run in evidence["runs"] if run["role"] == "producer")
        duplicate = json.loads(json.dumps(original))
        # Preserve the workflow-run object to prove that duplicate IDs/attempts cannot hide a
        # second SHA behind the first entry.
        duplicate["head_sha"] = OTHER_SHA
        evidence["runs"].append(duplicate)

        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())

        self.assertFalse(result.ok)
        self.assertTrue(any("exactly one" in reason for reason in result.reasons))
        self.assertTrue(any("SHA" in reason for reason in result.reasons))

    def test_shell_start_failure_in_a_gate_step_fails_closed(self) -> None:
        evidence = _load_pass_evidence()
        producer_run = next(r for r in evidence["runs"] if r["role"] == "producer")
        producer_run["steps"][0]["conclusion"] = "failure"
        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())
        self.assertFalse(result.ok)
        self.assertTrue(any("step" in reason.lower() for reason in result.reasons))

    def test_incomplete_pagination_fails_closed(self) -> None:
        evidence = _load_pass_evidence()
        evidence["pagination"]["complete"] = False
        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())
        self.assertFalse(result.ok)
        self.assertTrue(any("pagination" in reason for reason in result.reasons))

    def test_wrong_provider_fails_closed(self) -> None:
        evidence = _load_pass_evidence()
        evidence["runs"][0]["app_id"] = 999
        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())
        self.assertFalse(result.ok)
        self.assertTrue(any("provider" in reason.lower() for reason in result.reasons))

    def test_consumer_gitlink_must_equal_core_sha(self) -> None:
        evidence = _load_pass_evidence()
        for run in evidence["runs"]:
            if run["role"] in ("consumer", "promotion"):
                run["umbrella_gitlink_sha"] = OTHER_SHA
        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())
        self.assertFalse(result.ok)
        self.assertTrue(any("gitlink" in reason.lower() for reason in result.reasons))

    def test_umbrella_sha_need_not_equal_core_sha(self) -> None:
        evidence = _load_pass_evidence()
        self.assertNotEqual(
            evidence["reviewed_shas"]["core"], evidence["reviewed_shas"]["umbrella"]
        )
        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())
        self.assertTrue(result.ok, result.reasons)

    def test_evidence_cannot_redefine_the_required_identity_set(self) -> None:
        evidence = _load_pass_evidence()
        # Drop a producer run but also shrink an attacker-supplied "required" list.
        evidence["required"] = ["ruff + mypy (incl. complexity)"]
        evidence["runs"] = [
            run for run in evidence["runs"]
            if run["check_name"] == "ruff + mypy (incl. complexity)"
        ]
        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())
        self.assertFalse(result.ok)

    def test_post_apply_rules_are_checked_against_the_renderer(self) -> None:
        evidence = _load_pass_evidence()
        evidence["applied_rulesets"]["producer"]["rules"] = []
        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())
        self.assertFalse(result.ok)
        self.assertTrue(
            any("post-apply" in reason.lower() or "renderer" in reason.lower()
                for reason in result.reasons)
        )

    def test_wrong_schema_version_fails_closed(self) -> None:
        evidence = _load_pass_evidence()
        evidence["schema_version"] = 999
        result = rc.verify_acceptance_evidence(evidence, _contract(), _targets())
        self.assertFalse(result.ok)
        self.assertTrue(any("schema" in reason.lower() for reason in result.reasons))


class LiveAdapterIsReadOnlyAndOffline(unittest.TestCase):
    def test_fetch_check_runs_follows_pagination_from_fixtures(self) -> None:
        pages = [
            json.loads((FIXTURES / name).read_text(encoding="utf-8"))
            for name in ("check-runs-page-1.json", "check-runs-page-2.json")
        ]
        calls: list[tuple[str, str]] = []

        def opener(url: str, token: str):
            calls.append((url, token))
            index = len(calls) - 1
            nxt = "https://api.github.com/next" if index == 0 else None
            return pages[index], nxt

        with mock.patch.dict(os.environ, {"RC_TOK": "s3cr3t"}):
            body = rc.fetch_check_runs(
                PRODUCER_REPO, CORE_SHA, token_env="RC_TOK", opener=opener
            )
        self.assertEqual(body["total_count"], len(body["check_runs"]))
        self.assertEqual(calls[0][1], "s3cr3t")

    def test_absent_credential_fails_closed(self) -> None:
        os.environ.pop("RC_UNSET", None)
        with self.assertRaises(rc.EvidenceError):
            rc.fetch_check_runs(PRODUCER_REPO, CORE_SHA, token_env="RC_UNSET")

    def test_transport_error_is_wrapped(self) -> None:
        def opener(url: str, token: str):
            raise rc.EvidenceError("boom at " + url)

        with mock.patch.dict(os.environ, {"RC_TOK": "s3cr3t"}):
            with self.assertRaises(rc.EvidenceError):
                rc.fetch_check_runs(
                    PRODUCER_REPO, CORE_SHA, token_env="RC_TOK", opener=opener
                )

    def test_redaction_removes_the_token_value(self) -> None:
        text = "Authorization: Bearer s3cr3t-value failed"
        with mock.patch.dict(os.environ, {"RC_TOK": "s3cr3t-value"}):
            scrubbed = rc.redact(text, "RC_TOK")
        self.assertNotIn("s3cr3t-value", scrubbed)
        self.assertIn("***", scrubbed)

    def test_adapter_module_exposes_no_mutation_entry_points(self) -> None:
        for name in dir(rc):
            attr = name.lower()
            self.assertNotIn("dispatch", attr)
            if attr.startswith(("create_", "update_", "delete_", "put_", "post_")):
                self.fail(f"unexpected mutation entry point: {name}")


class OfflineCli(unittest.TestCase):
    def _report(self, directory: Path, evidence: dict) -> Path:
        report = directory / "acceptance.md"
        report.write_text(
            "# Remote acceptance\n\nnarrative\n\n```json\n"
            + json.dumps(evidence, indent=2)
            + "\n```\n",
            encoding="utf-8",
        )
        return report

    def _desired(self, directory: Path) -> Path:
        loaded = _contract()
        payload = {
            "producer": rc.render_desired_ruleset(
                rc.required_check_contract(loaded), _producer_target()
            ),
            "consumer": rc.render_desired_ruleset(
                rc.required_check_contract(loaded), _consumer_target()
            ),
        }
        path = directory / "desired_rulesets.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def test_valid_report_exits_zero(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            report = self._report(directory, _load_pass_evidence())
            desired = self._desired(directory)
            out = io.StringIO()
            code = rc.main(
                [
                    "verify",
                    "--source-root",
                    ".",
                    "--desired",
                    str(desired),
                    "--evidence",
                    str(report),
                ],
                stdout=out,
            )
        self.assertEqual(code, 0, out.getvalue())

    def test_invalid_shape_exits_nonzero(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            broken = _load_pass_evidence()
            broken["runs"][0]["conclusion"] = "failure"
            report = self._report(directory, broken)
            desired = self._desired(directory)
            out = io.StringIO()
            code = rc.main(
                [
                    "verify",
                    "--source-root",
                    ".",
                    "--desired",
                    str(desired),
                    "--evidence",
                    str(report),
                ],
                stdout=out,
            )
        self.assertNotEqual(code, 0)

    def test_missing_fenced_json_exits_two(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            report = directory / "acceptance.md"
            report.write_text("# no json here\n", encoding="utf-8")
            out = io.StringIO()
            code = rc.main(
                ["verify", "--source-root", ".", "--evidence", str(report)],
                stdout=out,
            )
        self.assertEqual(code, 2)

    def test_desired_mismatch_exits_nonzero(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            report = self._report(directory, _load_pass_evidence())
            desired = self._desired(directory)
            payload = json.loads(desired.read_text(encoding="utf-8"))
            payload["producer"]["rules"] = []
            desired.write_text(json.dumps(payload), encoding="utf-8")
            out = io.StringIO()
            code = rc.main(
                [
                    "verify",
                    "--source-root",
                    ".",
                    "--desired",
                    str(desired),
                    "--evidence",
                    str(report),
                ],
                stdout=out,
            )
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
