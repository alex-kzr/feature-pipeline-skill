"""UGA-07 - the core-owned same-SHA promotion verifier (``ci/promotion.py``) and the
umbrella ``core-promotion.yml`` adapter that invokes it.

A submodule gitlink update is a dependency promotion: it must be accepted only when every
required *producer* check is successful for the exact gitlink SHA, never for a branch head or
an unrelated commit. These tests pin, one rule at a time (Implementation Notes):

* fixture/offline mode over static GitHub *check-runs* API responses
  (``tests/fixtures/ci/promotion/``) - success, wrong SHA, missing/pending/failing checks,
  incomplete pagination, duplicate reruns, and a malformed body;
* the redacted JSON evidence names the umbrella SHA, gitlink SHA, upstream repository, required
  checks, per-check conclusions, and API URLs, and never a token (AC-4);
* live mode reads only the *name* of a token environment variable, never logs its value, and
  fails closed when the credential is absent (Requirements bullet 2/3);
* the required check identities come from ``ci/gates.toml`` and never include the umbrella
  ``installed-package`` consumer identity (AC-3);
* the committed ``core-promotion.yml`` resolves the exact ``git rev-parse HEAD:feature-pipeline-skill``
  gitlink, invokes the verifier on a gitlink change, and produces an explicit successful no-op
  otherwise, with read-only permissions (Requirements bullet 4/5).

Standard library only. No live HTTP request is ever made here.
"""

from __future__ import annotations

import io
import json
import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from ci import contract, promotion

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci" / "promotion"
PROMOTION_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "core-promotion.yml"
)

CORE_SHA = "1111111111111111111111111111111111111111"
OTHER_SHA = "2222222222222222222222222222222222222222"
UMBRELLA_SHA = "abcabcabcabcabcabcabcabcabcabcabcabcabcab"
SYNTH_CHECKS = ("lint-and-types", "coverage", "suite-a", "suite-b")
UPSTREAM = "example/feature-pipeline-skill"


def _payload(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _verify(name: str, *, required=SYNTH_CHECKS, core_sha: str = CORE_SHA):
    return promotion.verify_promotion(
        upstream_repo=UPSTREAM,
        core_sha=core_sha,
        required_checks=required,
        payload=_payload(name),
        umbrella_sha=UMBRELLA_SHA,
    )


class FixtureModeVerifier(unittest.TestCase):
    def test_all_required_checks_green_for_the_exact_sha_passes(self) -> None:
        result = _verify("success.json")
        self.assertTrue(result.ok, result.reasons)
        self.assertEqual(result.reasons, ())
        self.assertEqual(
            {c.identity for c in result.conclusions}, set(SYNTH_CHECKS)
        )

    def test_absent_core_sha_fails_closed(self) -> None:
        result = promotion.verify_promotion(
            upstream_repo=UPSTREAM,
            core_sha="",
            required_checks=SYNTH_CHECKS,
            payload=_payload("success.json"),
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("SHA" in r for r in result.reasons))

    def test_green_checks_for_a_different_sha_cannot_satisfy_the_gate(self) -> None:
        result = _verify("wrong-sha.json")
        self.assertFalse(result.ok)
        self.assertTrue(result.reasons)
        self.assertTrue(all("another SHA" in r for r in result.reasons))

    def test_missing_required_check_fails_closed(self) -> None:
        result = _verify("success.json", required=SYNTH_CHECKS + ("suite-z",))
        self.assertFalse(result.ok)
        self.assertTrue(any("suite-z" in r and "missing" in r for r in result.reasons))

    def test_pending_required_check_fails_closed(self) -> None:
        result = _verify("pending.json")
        self.assertFalse(result.ok)
        self.assertTrue(any("suite-b" in r and "completed" in r for r in result.reasons))

    def test_failing_required_check_fails_closed(self) -> None:
        result = _verify("failing.json")
        self.assertFalse(result.ok)
        self.assertTrue(any("suite-b" in r and "success" in r for r in result.reasons))

    def test_incomplete_pagination_fails_closed(self) -> None:
        result = _verify("incomplete-pagination.json")
        self.assertFalse(result.ok)
        self.assertTrue(any("pagination" in r for r in result.reasons))

    def test_malformed_response_fails_closed(self) -> None:
        result = _verify("malformed-not-object.json")
        self.assertFalse(result.ok)
        self.assertTrue(result.reasons)

    def test_duplicate_rerun_uses_the_latest_completed_run(self) -> None:
        # An early `failure` for `suite-a` superseded by a later `success` rerun -> passes,
        # selected deterministically by completed_at then id.
        result = _verify("duplicate-reruns.json")
        self.assertTrue(result.ok, result.reasons)
        suite_a = next(c for c in result.conclusions if c.identity == "suite-a")
        self.assertEqual(suite_a.conclusion, "success")


class RedactedEvidence(unittest.TestCase):
    def test_evidence_names_every_required_field_and_no_token(self) -> None:
        result = _verify("success.json")
        evidence = result.evidence()
        for key in (
            "umbrella_sha",
            "gitlink_sha",
            "upstream_repo",
            "required_checks",
            "conclusions",
            "api_urls",
        ):
            self.assertIn(key, evidence)
        self.assertEqual(evidence["gitlink_sha"], CORE_SHA)
        self.assertEqual(evidence["umbrella_sha"], UMBRELLA_SHA)
        self.assertEqual(evidence["upstream_repo"], UPSTREAM)
        self.assertTrue(evidence["api_urls"])
        blob = json.dumps(evidence).lower()
        for leak in ("authorization", "bearer", "token", "ghp_", "secret"):
            self.assertNotIn(leak, blob)

    def test_cli_scrubs_the_token_value_if_it_ever_appears_in_output(self) -> None:
        with TemporaryDirectory() as raw:
            leaky = Path(raw) / "leaky.json"
            body = _payload("success.json")
            body["api_urls"] = [
                "https://api.github.com/repos/example/feature-pipeline-skill/"
                "commits/1111111111111111111111111111111111111111/check-runs?x=SEKRET"
            ]
            leaky.write_text(json.dumps(body), encoding="utf-8")
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"PROMO_TOK": "SEKRET"}):
                code = promotion.main(
                    [
                        "--repo",
                        UPSTREAM,
                        "--sha",
                        CORE_SHA,
                        "--umbrella-sha",
                        UMBRELLA_SHA,
                        "--mode",
                        "fixture",
                        "--fixture",
                        str(leaky),
                        "--token-env",
                        "PROMO_TOK",
                        "--required-check",
                        "lint-and-types",
                        "--required-check",
                        "coverage",
                        "--required-check",
                        "suite-a",
                        "--required-check",
                        "suite-b",
                    ],
                    stdout=out,
                )
        self.assertEqual(code, 0)
        self.assertNotIn("SEKRET", out.getvalue())
        self.assertIn("***", out.getvalue())


class CliExitCodes(unittest.TestCase):
    def _run(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        code = promotion.main(list(args), stdout=out)
        return code, out.getvalue()

    def _fixture_args(self, fixture: str) -> list[str]:
        return [
            "--repo",
            UPSTREAM,
            "--sha",
            CORE_SHA,
            "--mode",
            "fixture",
            "--fixture",
            str(FIXTURES / fixture),
            *sum((["--required-check", c] for c in SYNTH_CHECKS), []),
        ]

    def test_success_fixture_exits_zero(self) -> None:
        code, text = self._run(*self._fixture_args("success.json"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["ok"], True)

    def test_failing_fixture_exits_one(self) -> None:
        code, text = self._run(*self._fixture_args("failing.json"))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)["ok"], False)

    def test_missing_fixture_path_exits_two(self) -> None:
        out = io.StringIO()
        code = promotion.main(
            ["--repo", UPSTREAM, "--sha", CORE_SHA, "--mode", "fixture"], stdout=out
        )
        self.assertEqual(code, 2)


class LiveModeReadsOnlyTheTokenName(unittest.TestCase):
    def test_absent_credential_fails_closed(self) -> None:
        os.environ.pop("UGA07_UNSET_TOKEN", None)
        with self.assertRaises(promotion.PromotionError):
            promotion.fetch_check_runs(
                UPSTREAM, CORE_SHA, token_env="UGA07_UNSET_TOKEN"
            )

    def test_token_value_is_passed_to_the_opener_not_logged(self) -> None:
        seen: list[tuple[str, str]] = []

        def opener(url: str, token: str):
            seen.append((url, token))
            return {"total_count": 0, "check_runs": []}, None

        with mock.patch.dict(os.environ, {"UGA07_TOK": "s3cr3t-value"}):
            payload = promotion.fetch_check_runs(
                UPSTREAM, CORE_SHA, token_env="UGA07_TOK", opener=opener
            )
        self.assertEqual(payload["check_runs"], [])
        self.assertEqual(seen[0][1], "s3cr3t-value")
        self.assertIn(CORE_SHA, seen[0][0])

    def test_incomplete_live_pagination_is_rejected_by_verify(self) -> None:
        def opener(url: str, token: str):
            return (
                {
                    "total_count": 4,
                    "check_runs": [
                        {
                            "id": 1,
                            "name": "lint-and-types",
                            "head_sha": CORE_SHA,
                            "status": "completed",
                            "conclusion": "success",
                            "completed_at": "2026-09-06T10:03:00Z",
                            "html_url": "https://github.com/example/x/runs/1",
                        }
                    ],
                },
                None,
            )

        with mock.patch.dict(os.environ, {"UGA07_TOK": "s"}):
            payload = promotion.fetch_check_runs(
                UPSTREAM, CORE_SHA, token_env="UGA07_TOK", opener=opener
            )
        result = promotion.verify_promotion(
            upstream_repo=UPSTREAM,
            core_sha=CORE_SHA,
            required_checks=("lint-and-types",),
            payload=payload,
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("pagination" in r for r in result.reasons))


class RequiredChecksComeFromTheManifest(unittest.TestCase):
    def test_identities_are_derived_from_gates_toml_core_group(self) -> None:
        required = promotion.required_core_checks(contract.load())
        self.assertEqual(
            required,
            (
                "ruff + mypy (incl. complexity)",
                "coverage (branch, ratcheted floor)",
                "platform · ubuntu-latest",
                "platform · windows-latest",
                "fault-injection · ubuntu-latest",
                "fault-injection · windows-latest",
                "performance · ubuntu-latest",
                "performance · windows-latest",
            ),
        )

    def test_consumer_installed_package_identity_is_kept_separate(self) -> None:
        required = promotion.required_core_checks(contract.load())
        self.assertNotIn("installed-package", required)
        self.assertNotIn(promotion.CONSUMER_CHECK_IDENTITY, required)
        self.assertEqual(promotion.CONSUMER_CHECK_IDENTITY, "installed-package")


class PromotionWorkflowAdapter(unittest.TestCase):
    def setUp(self) -> None:
        if not PROMOTION_WORKFLOW.is_file():
            self.fail(f"missing promotion workflow {PROMOTION_WORKFLOW}")
        self.text = PROMOTION_WORKFLOW.read_text(encoding="utf-8")

    def test_identity_is_its_own_stable_name(self) -> None:
        self.assertIn("name: core-promotion", self.text)
        self.assertNotIn("name: installed-package", self.text)

    def test_permissions_are_read_only(self) -> None:
        block = re.search(r"\npermissions:\n((?:  .*\n)+)", self.text)
        self.assertIsNotNone(block)
        self.assertEqual(block.group(1).strip(), "contents: read")
        self.assertNotIn("write", block.group(0))

    def test_resolves_the_exact_gitlink_sha(self) -> None:
        self.assertIn("git rev-parse HEAD:feature-pipeline-skill", self.text)
        self.assertNotIn("origin/main", self.text)
        self.assertNotIn("ls-remote", self.text)

    def test_non_gitlink_change_is_an_explicit_success_no_op(self) -> None:
        self.assertRegex(
            self.text, r"if:\s*steps\.gitlink\.outputs\.gitlink-changed != 'yes'"
        )
        self.assertIn("no-op", self.text)

    def test_gitlink_change_invokes_the_verifier(self) -> None:
        self.assertRegex(
            self.text, r"if:\s*steps\.gitlink\.outputs\.gitlink-changed == 'yes'"
        )
        self.assertIn("python -m ci.promotion", self.text)
        self.assertIn("--mode live", self.text)
        self.assertIn("--token-env PROMOTION_TOKEN", self.text)
        self.assertIn('--sha "${{ steps.gitlink.outputs.core-sha }}"', self.text)

    def test_token_is_passed_by_env_name_never_interpolated_into_a_command(self) -> None:
        self.assertIn(
            "PROMOTION_TOKEN: ${{ secrets.CORE_STATUS_TOKEN || github.token }}", self.text
        )
        self.assertNotIn("echo \"$PROMOTION_TOKEN", self.text)
        self.assertNotIn("--token-env ${{ secrets", self.text)

    def test_no_repository_name_derived_working_directory(self) -> None:
        self.assertNotIn("working-directory:", self.text)


if __name__ == "__main__":
    unittest.main()
