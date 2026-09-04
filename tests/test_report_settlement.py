"""VP-02 — one parameterized settlement parser, typed findings/dispositions, mechanical gates.

The pre-VP-02 behaviour is the reference: ``pipeline_core.reports`` still exposes
``parse_executor_status`` / ``parse_status_envelope`` / ``settle_executor_status`` and the
verdict trio, with byte-identical messages and ``code`` slugs. These tests pin that the two
public paths now route through the **one** implementation in
``feature_pipeline.reports.settlement`` and that the malformed / truncated / stale / conflicting
envelope inputs settle identically on both.
"""

from __future__ import annotations

import inspect
import json
import unittest

from feature_pipeline.application import report_gates
from feature_pipeline.reports import dispositions, findings, renderers, settlement
from pipeline_core import reporting, reports


def _envelope(token_field: str, token: str, **over: object) -> str:
    body: dict[str, object] = {
        "role": "executor" if token_field == "status" else "task_verifier",
        token_field: token,
        "task_id": "VP-02",
        "attempt": 1,
    }
    body.update(over)
    return json.dumps(body)


# --------------------------------------------------------------------------------------------
# AC-1 — one settlement implementation handles every supported report envelope
# --------------------------------------------------------------------------------------------
class OneSettlementImplementation(unittest.TestCase):
    def test_public_parsers_delegate_to_the_one_module(self) -> None:
        for fn in (
            reports.parse_executor_status,
            reports.parse_status_envelope,
            reports.settle_executor_status,
            reports.parse_verifier_verdict,
            reports.parse_verdict_envelope,
            reports.settle_verifier_verdict,
        ):
            src = inspect.getsource(fn)
            self.assertTrue(
                "_parse_prose_token" in src
                or "_parse_envelope" in src
                or "_settle(" in src,
                f"{fn.__name__} still has its own parser body",
            )

    def test_contracts_registry_covers_both_supported_envelopes(self) -> None:
        self.assertEqual(set(settlement.CONTRACTS), {"status", "verdict"})
        self.assertEqual(settlement.STATUS_CONTRACT.token_values, ("implemented", "blocked"))
        self.assertEqual(
            settlement.VERDICT_CONTRACT.token_values, ("PASS", "FAIL", "BLOCKED")
        )

    def test_constants_are_sourced_from_the_contracts(self) -> None:
        self.assertIs(reports.STATUS_TOKENS, settlement.STATUS_CONTRACT.token_values)
        self.assertIs(reports.ENVELOPE_KEYS, settlement.STATUS_CONTRACT.keys)
        self.assertIs(reports.VERDICT_TOKENS, settlement.VERDICT_CONTRACT.token_values)
        self.assertIs(reports.VERDICT_ENVELOPE_KEYS, settlement.VERDICT_CONTRACT.keys)


# --------------------------------------------------------------------------------------------
# AC-2 — reusable contracts cover malformed and stale results (parity, both paths)
# --------------------------------------------------------------------------------------------
_MALFORMED = [
    "",  # empty
    "   ",  # whitespace only
    "not json at all",  # unparseable
    '{"role": "executor", "status": "implemented"',  # truncated JSON
    "[1, 2, 3]",  # JSON but not an object
    '{"role": "executor", "status": "implemented", "task_id": "VP-02"}',  # missing key
    '{"role": "x", "status": "implemented", "task_id": "VP-02", "attempt": 1, "extra": 1}',
    '{"role": "executor", "status": "done", "task_id": "VP-02", "attempt": 1}',  # bad token
    '{"role": "other", "status": "implemented", "task_id": "VP-02", "attempt": 1}',  # role
    '{"role": "executor", "status": "implemented", "task_id": "OTHER", "attempt": 1}',  # stale id
    '{"role": "executor", "status": "implemented", "task_id": "VP-02", "attempt": 2}',  # stale n
    '{"role": "executor", "status": "implemented", "task_id": "VP-02", "attempt": true}',
]


class MalformedAndStaleParity(unittest.TestCase):
    def test_status_envelope_parity(self) -> None:
        for text in _MALFORMED:
            with self.subTest(text=text):
                with self.assertRaises(reports.ReportError) as legacy:
                    reports.parse_status_envelope(
                        text, role="executor", task_id="VP-02", attempt=1
                    )
                with self.assertRaises(settlement.ReportProtocolError) as one:
                    settlement.parse_envelope(
                        text,
                        settlement.STATUS_CONTRACT,
                        role="executor",
                        task_id="VP-02",
                        attempt=1,
                    )
                self.assertEqual(legacy.exception.code, one.exception.code)
                self.assertEqual(str(legacy.exception), str(one.exception))
                self.assertEqual(legacy.exception.code, "unparseable-status-envelope")

    def test_verdict_envelope_parity(self) -> None:
        for text in _MALFORMED:
            fixed = text.replace('"status"', '"verdict"').replace(
                '"executor"', '"task_verifier"'
            )
            with self.subTest(text=fixed):
                with self.assertRaises(reports.ReportError) as legacy:
                    reports.parse_verdict_envelope(
                        fixed, role="task_verifier", task_id="VP-02", attempt=1
                    )
                with self.assertRaises(settlement.ReportProtocolError) as one:
                    settlement.parse_envelope(
                        fixed,
                        settlement.VERDICT_CONTRACT,
                        role="task_verifier",
                        task_id="VP-02",
                        attempt=1,
                    )
                self.assertEqual(legacy.exception.code, one.exception.code)
                self.assertEqual(str(legacy.exception), str(one.exception))

    def test_prose_parser_parity(self) -> None:
        for text in ("the work is done", "", "Status : maybe"):
            with self.assertRaises(reports.ReportError) as legacy:
                reports.parse_executor_status(text)
            with self.assertRaises(settlement.ReportProtocolError) as one:
                settlement.parse_prose_token(text, settlement.STATUS_CONTRACT)
            self.assertEqual(legacy.exception.code, one.exception.code)
            self.assertEqual(str(legacy.exception), str(one.exception))

    def test_valid_status_envelope_round_trips_on_both_paths(self) -> None:
        text = _envelope("status", "implemented")
        self.assertEqual(
            reports.parse_status_envelope(
                text, role="executor", task_id="VP-02", attempt=1
            ),
            "implemented",
        )
        self.assertEqual(
            settlement.parse_envelope(
                text, settlement.STATUS_CONTRACT, role="executor", task_id="VP-02", attempt=1
            ),
            "implemented",
        )

    def test_conflicting_prose_and_envelope_fail_closed_identically(self) -> None:
        with self.assertRaises(reports.ReportError) as legacy:
            reports.settle_executor_status(
                prose_text="- Status: blocked",
                envelope_text=_envelope("status", "implemented"),
                role="executor",
                task_id="VP-02",
                attempt=1,
            )
        with self.assertRaises(settlement.ReportProtocolError) as one:
            settlement.settle(
                settlement.STATUS_CONTRACT,
                prose_text="- Status: blocked",
                envelope_text=_envelope("status", "implemented"),
                role="executor",
                task_id="VP-02",
                attempt=1,
            )
        self.assertEqual(legacy.exception.code, "status-envelope-mismatch")
        self.assertEqual(str(legacy.exception), str(one.exception))

    def test_absent_prose_line_settles_to_envelope_with_drift(self) -> None:
        resolution = reports.settle_verifier_verdict(
            prose_text="no verdict here",
            envelope_text=_envelope("verdict", "PASS"),
            role="task_verifier",
            task_id="VP-02",
            attempt=1,
        )
        self.assertEqual(resolution.token, "PASS")
        self.assertIsNotNone(resolution.drift)


# --------------------------------------------------------------------------------------------
# Typed findings, outcomes, and the standardized disposition vocabulary
# --------------------------------------------------------------------------------------------
class TypedFindingsAndDispositions(unittest.TestCase):
    def test_finding_record_only_carries_set_optional_fields(self) -> None:
        bare = findings.Finding(code="x", message="m")
        self.assertEqual(
            bare.as_record(), {"code": "x", "message": "m", "severity": "major"}
        )
        full = findings.Finding(
            code="x",
            message="m",
            severity=findings.Severity.BLOCKER,
            location="a/b",
            criterion="AC-1",
            remediation="do y",
        )
        self.assertEqual(full.as_record()["criterion"], "AC-1")
        self.assertEqual(full.as_record()["severity"], "blocker")

    def test_dispositions_are_the_four_required_failures_plus_clean(self) -> None:
        self.assertEqual(
            {d.value for d in dispositions.Disposition},
            {"clean", "timeout", "protocol", "infrastructure-blocker", "product-failure"},
        )

    def test_classify_command_matches_pipeline_core_classify_outcome(self) -> None:
        from pipeline_core.commands import (
            DISPOSITION_BLOCKED,
            DISPOSITION_FAIL,
            DISPOSITION_PASS,
            classify_outcome,
        )
        from pipeline_core.state import EXIT_LAUNCH_FAILED, EXIT_NOT_FOUND, EXIT_TIMEOUT

        cases = {
            0: (DISPOSITION_PASS, dispositions.Disposition.CLEAN),
            EXIT_NOT_FOUND: (DISPOSITION_BLOCKED, dispositions.Disposition.INFRASTRUCTURE_BLOCKER),
            EXIT_LAUNCH_FAILED: (
                DISPOSITION_BLOCKED,
                dispositions.Disposition.INFRASTRUCTURE_BLOCKER,
            ),
            EXIT_TIMEOUT: (DISPOSITION_FAIL, dispositions.Disposition.TIMEOUT),
            2: (DISPOSITION_FAIL, dispositions.Disposition.PRODUCT_FAILURE),
        }
        for exit_code, (legacy_token, disp) in cases.items():
            legacy_disp, _ = classify_outcome(exit_code)
            self.assertEqual(legacy_disp, legacy_token)
            got = dispositions.classify_command(
                exit_code,
                timed_out=exit_code == EXIT_TIMEOUT,
                unavailable=exit_code == EXIT_NOT_FOUND,
                launch_failed=exit_code == EXIT_LAUNCH_FAILED,
            )
            self.assertEqual(got, disp)
            self.assertEqual(dispositions.to_verdict(got), legacy_token)

    def test_only_infrastructure_blocker_is_external(self) -> None:
        self.assertTrue(
            dispositions.is_external_blocker(
                dispositions.Disposition.INFRASTRUCTURE_BLOCKER
            )
        )
        for other in (
            dispositions.Disposition.TIMEOUT,
            dispositions.Disposition.PROTOCOL,
            dispositions.Disposition.PRODUCT_FAILURE,
        ):
            self.assertFalse(dispositions.is_external_blocker(other))


# --------------------------------------------------------------------------------------------
# AC-3 — deterministic mechanical gates; both verifier PASS required; repair re-runs the gate
# --------------------------------------------------------------------------------------------
class MechanicalGates(unittest.TestCase):
    def test_command_exit_gate_clean_when_every_record_exited_zero(self) -> None:
        out = report_gates.command_exit_gate(
            [{"id": "c1", "exit_code": 0, "argv": ["pytest"], "cwd": "."}]
        )
        self.assertTrue(out.clean)
        self.assertEqual(out.findings, ())

    def test_command_exit_gate_classifies_each_failure(self) -> None:
        out = report_gates.command_exit_gate(
            [
                {"id": "c1", "exit_code": 1, "argv": ["a"], "cwd": ".", "reason": "command exited with 1"},
                {"id": "c2", "exit_code": "not-found", "argv": ["b"], "cwd": ".",
                 "reason": "program or toolchain is not available"},
            ]
        )
        self.assertFalse(out.clean)
        codes = {f.code for f in out.findings}
        self.assertIn("command-product-failure", codes)
        self.assertIn("command-infrastructure-blocker", codes)

    def test_evidence_presence_gate_matches_missing_command_evidence(self) -> None:
        from pipeline_core.verification import missing_command_evidence

        claimed = [{"cwd": ".", "argv": ["ruff", "check"]}]
        records = [{"cwd": ".", "argv": ["pytest"]}]
        legacy = missing_command_evidence(claimed, records)
        gate = report_gates.evidence_presence_gate(claimed, records)
        self.assertEqual(len(legacy), len(gate.findings))
        self.assertFalse(gate.clean)
        self.assertEqual(gate.disposition, dispositions.Disposition.PRODUCT_FAILURE)

        clean = report_gates.evidence_presence_gate(
            [{"cwd": ".", "argv": ["pytest"]}], records
        )
        self.assertTrue(clean.clean)

    def test_protocol_gate_flags_a_malformed_envelope(self) -> None:
        out = report_gates.protocol_gate(
            settlement.STATUS_CONTRACT,
            prose_text="- Status: implemented",
            envelope_text="not json",
            role="executor",
            task_id="VP-02",
            attempt=1,
        )
        self.assertEqual(out.disposition, dispositions.Disposition.PROTOCOL)
        self.assertEqual(out.findings[0].code, "unparseable-status-envelope")

    def test_scope_gate_maps_the_wt02_decision(self) -> None:
        clean = report_gates.scope_gate(type("D", (), {"outcome": "clean"})())
        self.assertTrue(clean.clean)
        unavailable = report_gates.scope_gate(
            type("D", (), {"outcome": "attribution-unavailable", "reason": "snapshot lost"})()
        )
        self.assertEqual(
            unavailable.disposition, dispositions.Disposition.INFRASTRUCTURE_BLOCKER
        )
        violation = report_gates.scope_gate(
            type(
                "D",
                (),
                {"outcome": "scope-violation", "violations": [type("V", (), {"path": "x.py"})()]},
            )()
        )
        self.assertEqual(violation.disposition, dispositions.Disposition.PRODUCT_FAILURE)
        self.assertEqual(violation.findings[0].location, "x.py")

    def test_both_verifiers_must_pass(self) -> None:
        self.assertTrue(report_gates.both_verifiers_pass("PASS", "PASS"))
        self.assertFalse(report_gates.both_verifiers_pass("PASS", "FAIL"))
        self.assertFalse(report_gates.both_verifiers_pass("BLOCKED", "PASS"))


# --------------------------------------------------------------------------------------------
# Compatibility renderers — the historical Markdown is byte-identical
# --------------------------------------------------------------------------------------------
class CompatibilityRenderers(unittest.TestCase):
    def test_reporting_module_reexports_the_one_renderer(self) -> None:
        self.assertIs(reporting.render_report, renderers.render_report)
        self.assertIs(reporting.Section, renderers.Section)
        self.assertIs(reporting.fenced, renderers.fenced)

    def test_render_report_shape_is_unchanged(self) -> None:
        out = renderers.render_report(
            "Title", [renderers.Section("A", "line 1\nline 2"), renderers.Section("B", "x")]
        )
        self.assertEqual(
            out,
            "# Title\n\n## A\n\nline 1\nline 2\n\n## B\n\nx\n",
        )
        self.assertFalse(any(line != line.rstrip() for line in out.splitlines()))

    def test_fenced_guarantees_a_body(self) -> None:
        self.assertEqual(renderers.fenced(""), "```text\nnone\n```")


if __name__ == "__main__":
    unittest.main()
