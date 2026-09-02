"""VR-01 — the runner runs declared verification commands and packages one immutable
evidence set that both independent verifiers receive byte-for-byte.

The tests exercise four properties the contract names:

* every declared command runs in order and is recorded through the RDS-02 runtime, and a
  real *failure* never stops the pass;
* an *external blocker* (an unavailable program) halts the pass safely, retaining the
  evidence already gathered and marking the declared checks that were not run;
* a check the executor *claimed* with no matching runner-recorded command is surfaced as
  missing evidence a verifier can fail on without running a tool;
* the serialized evidence payload is deterministic and immutable, so the task-verifier and
  the test-verifier request builders are handed identical facts.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from pipeline_core.commands import (
    DISPOSITION_BLOCKED,
    DISPOSITION_FAIL,
    DISPOSITION_PASS,
    VerificationRun,
    run_command,
    run_verification_commands,
    verification_stage,
)
from pipeline_core.state import Run
from pipeline_core.verification import (
    VerificationEvidence,
    build_verification_evidence,
    evidence_forces_fail,
    missing_command_evidence,
    verifier_evidence_payload,
)
from schemas.contracts import CommandSpec


def _run(root: Path) -> Run:
    prompt = root / "prompt.md"
    prompt.write_text("prompt", encoding="utf-8")
    run = Run.create("verify", prompt, None, root / "runs" / "verify", root)
    run.add_task("VR-01")
    return run


def _emit(text: str, exit_code: int = 0) -> list[str]:
    return [sys.executable, "-c",
            f"import sys; sys.stdout.write({text!r}); sys.exit({exit_code})"]


def _record_execution_evidence(run: Run) -> None:
    """Give VR-01 a trusted executor report plus an attributed implementation delta."""
    report = run.run_dir / "reports" / "VR-01" / "launch-1" / "executor-1.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("- Status: implemented\n", encoding="utf-8")
    run.record_executor_evidence(
        "VR-01", attempt=1, generation=1, report_path=report, session_id="sess-1")
    run.record_implementation_attribution(
        "VR-01",
        generation=1,
        attempt=1,
        attribution_state="known",
        manifest="runs/verify/reports/VR-01/launch-1/implementation-manifest-1.json",
        diff="runs/verify/reports/VR-01/launch-1/implementation-diff-1.md",
        changed_files=[
            {
                "path": "feature-pipeline-skill/pipeline_core/verification.py",
                "status": "modified",
                "digest": "sha256:abc",
                "classification": "in_allowed_scope",
            }
        ],
    )


class OrderedExecutionTests(unittest.TestCase):
    def test_every_declared_command_runs_in_order_and_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            stage = verification_stage("VR-01", attempt=1)
            commands = (
                CommandSpec(".", tuple(_emit("first"))),
                CommandSpec(".", tuple(_emit("second"))),
            )

            result = run_verification_commands(
                run, commands, stage=stage, task_id="VR-01", attempt=1)

            self.assertIsInstance(result, VerificationRun)
            self.assertTrue(result.complete)
            self.assertEqual([r["id"] for r in result.records], ["command-1", "command-2"])
            self.assertEqual([r["command_index"] for r in result.records], [1, 2])
            self.assertEqual(run.stage_command_ids(stage), ["command-1", "command-2"])
            self.assertEqual(
                [r["disposition"] for r in result.records],
                [DISPOSITION_PASS, DISPOSITION_PASS],
            )
            for record in result.records:
                self.assertEqual(record["task_id"], "VR-01")
                self.assertEqual(record["attempt"], 1)

    def test_a_failing_command_does_not_stop_the_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            stage = verification_stage("VR-01", attempt=1)
            commands = (
                CommandSpec(".", tuple(_emit("boom", 1))),
                CommandSpec(".", tuple(_emit("still-ran"))),
            )

            result = run_verification_commands(run, commands, stage=stage)

            self.assertTrue(result.complete)
            self.assertEqual(len(result.records), 2)
            self.assertEqual(result.records[0]["disposition"], DISPOSITION_FAIL)
            self.assertEqual(result.records[1]["disposition"], DISPOSITION_PASS)
            self.assertEqual(result.unrun, ())


class ExternalBlockerTests(unittest.TestCase):
    def test_an_unavailable_program_halts_the_pass_and_retains_partial_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            stage = verification_stage("VR-01", attempt=1)
            commands = (
                CommandSpec(".", ("definitely-not-a-real-program-xyz", "--check")),
                CommandSpec(".", tuple(_emit("never-reached"))),
            )

            result = run_verification_commands(run, commands, stage=stage)

            self.assertFalse(result.complete)
            self.assertEqual(len(result.records), 1)
            self.assertEqual(result.records[0]["disposition"], DISPOSITION_BLOCKED)
            self.assertEqual(len(result.unrun), 1)
            self.assertEqual(
                result.unrun[0], (".", tuple(_emit("never-reached"))))
            self.assertIn("not available", result.stopped_reason)

            _record_execution_evidence(run)
            evidence = build_verification_evidence(
                run, "VR-01", attempt=1, commands_run=result)

            self.assertFalse(evidence.complete)
            self.assertEqual(evidence.external_blocker, result.stopped_reason)
            self.assertEqual(len(evidence.commands), 1)
            self.assertEqual(len(evidence.unrun_commands), 1)
            # every declared check maps to a record or an explicit blocker entry (AC-1)
            self.assertEqual(
                len(evidence.commands) + len(evidence.unrun_commands), len(commands))

    def test_evidence_references_logs_it_does_not_inline_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            stage = verification_stage("VR-01", attempt=1)
            # The output text is computed by the script, so it never appears in argv.
            emit = [sys.executable, "-c", "import sys; sys.stdout.write(chr(90) * 64)"]
            result = run_verification_commands(
                run, (CommandSpec(".", tuple(emit)),), stage=stage)
            _record_execution_evidence(run)

            evidence = build_verification_evidence(
                run, "VR-01", attempt=1, commands_run=result)

            reference = evidence.commands[0]
            self.assertIn("stdout_log", reference)
            self.assertNotIn("stdout", reference)
            self.assertNotIn("stderr", reference)
            self.assertNotIn("Z" * 64, evidence.serialized())


class MissingClaimTests(unittest.TestCase):
    def test_a_claimed_check_with_no_recorded_command_is_missing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            stage = verification_stage("VR-01", attempt=1)
            ran = CommandSpec(".", tuple(_emit("ran")))
            result = run_verification_commands(run, (ran,), stage=stage)
            _record_execution_evidence(run)

            claimed = [
                {"cwd": ".", "argv": list(ran.argv)},
                {"cwd": ".", "argv": ["pytest", "-q", "tests/test_that_was_never_run.py"]},
            ]
            evidence = build_verification_evidence(
                run, "VR-01", attempt=1, commands_run=result, claimed_checks=claimed)

            self.assertEqual(len(evidence.missing_evidence), 1)
            self.assertEqual(
                evidence.missing_evidence[0]["argv"],
                ["pytest", "-q", "tests/test_that_was_never_run.py"],
            )
            reason = evidence_forces_fail(evidence)
            self.assertIsNotNone(reason)
            self.assertIn("no runner-recorded command", reason)

    def test_fully_backed_claims_leave_no_missing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            stage = verification_stage("VR-01", attempt=1)
            ran = CommandSpec(".", tuple(_emit("ran")))
            result = run_verification_commands(run, (ran,), stage=stage)
            _record_execution_evidence(run)

            evidence = build_verification_evidence(
                run, "VR-01", attempt=1, commands_run=result,
                claimed_checks=[{"cwd": ".", "argv": list(ran.argv)}])

            self.assertEqual(evidence.missing_evidence, ())
            self.assertIsNone(evidence_forces_fail(evidence))

    def test_missing_command_evidence_is_a_pure_function(self) -> None:
        records = [{"cwd": "pkg", "argv": ["make", "check"]}]
        claimed = [
            CommandSpec("pkg", ("make", "check")),
            CommandSpec("pkg", ("make", "lint")),
        ]
        missing = missing_command_evidence(claimed, records)
        self.assertEqual([entry["argv"] for entry in missing], [["make", "lint"]])


class SerializedPayloadTests(unittest.TestCase):
    def _evidence(self, root: Path, *, attempt: int = 1) -> VerificationEvidence:
        run = _run(root)
        stage = verification_stage("VR-01", attempt=attempt)
        result = run_verification_commands(
            run, (CommandSpec(".", tuple(_emit("ok"))),), stage=stage,
            task_id="VR-01", attempt=attempt)
        _record_execution_evidence(run)
        return build_verification_evidence(
            run, "VR-01", attempt=attempt, commands_run=result)

    def test_both_verifier_builders_receive_identical_evidence_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._evidence(Path(directory))

            payload = verifier_evidence_payload(evidence)
            self.assertEqual(payload, verifier_evidence_payload(evidence))

            task_request = f"[task-verifier]\n{payload}"
            test_request = f"[test-verifier]\n{payload}"
            self.assertEqual(
                task_request.split("\n", 1)[1], test_request.split("\n", 1)[1])
            # the payload is valid JSON carrying the binding facts (AC-2)
            decoded = json.loads(payload)
            self.assertEqual(decoded["task_id"], "VR-01")
            self.assertEqual(decoded["attempt"], 1)
            self.assertTrue(
                decoded["implementation"]["manifest"].endswith(
                    "implementation-manifest-1.json"))
            self.assertTrue(
                decoded["implementation"]["diff"].endswith("implementation-diff-1.md"))
            self.assertTrue(decoded["executor_report"].endswith("executor-1.md"))

    def test_payload_changes_with_the_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = verifier_evidence_payload(self._evidence(Path(a), attempt=1))
            second = verifier_evidence_payload(self._evidence(Path(b), attempt=2))
            self.assertNotEqual(first, second)

    def test_evidence_package_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._evidence(Path(directory))
            self.assertIsInstance(evidence.commands, tuple)
            self.assertIsInstance(evidence.missing_evidence, tuple)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                evidence.attempt = 9  # type: ignore[misc]


class EvidenceBindingTests(unittest.TestCase):
    def test_evidence_binds_commands_to_task_attempt_and_implementation_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            stage = verification_stage("VR-01", attempt=2)
            result = run_verification_commands(
                run, (CommandSpec(".", tuple(_emit("ok"))),), stage=stage,
                task_id="VR-01", attempt=2)
            _record_execution_evidence(run)

            evidence = build_verification_evidence(
                run, "VR-01", attempt=2, commands_run=result)

            self.assertEqual(evidence.task_id, "VR-01")
            self.assertEqual(evidence.attempt, 2)
            self.assertTrue(evidence.executor_report.endswith("executor-1.md"))
            self.assertTrue(
                evidence.implementation_manifest.endswith(
                    "implementation-manifest-1.json"))
            self.assertTrue(
                evidence.implementation_diff.endswith("implementation-diff-1.md"))
            self.assertEqual(
                [row["path"] for row in evidence.changed_files],
                ["feature-pipeline-skill/pipeline_core/verification.py"],
            )
            self.assertEqual(evidence.commands[0]["task_id"], "VR-01")
            self.assertEqual(evidence.commands[0]["attempt"], 2)


# --- encoding (RDS-12) --------------------------------------------------------------------------


#: Writes real, hardcoded UTF-8-encoded non-ASCII bytes straight to the stdout/stderr file
#: descriptors — exactly how a compiler, linter, or test runner behaves on a redirected pipe
#: regardless of the host's locale. Kept pure-ASCII (``\uXXXX`` escapes, raw buffer writes) so
#: only the parent's decoding of these bytes is under test, never the child's own stdout codec.
_UTF8_STDIO_SCRIPT = (
    "import sys\n"
    "sys.stdout.buffer.write('caf\\u00e9 \\u2014 na\\u00efve'.encode('utf-8'))\n"
    "sys.stderr.buffer.write('na\\u00efve \\u2014 caf\\u00e9'.encode('utf-8'))\n"
)


class SubprocessEncodingTests(unittest.TestCase):
    """RDS-12: ``run_command``'s launcher must pin UTF-8 rather than fall back to
    ``locale.getpreferredencoding()`` — a non-UTF-8 host locale (verified: ``cp1251``) silently
    corrupts every non-ASCII byte a verification command writes to stdout/stderr. The locale is
    patched to a "wrong" codepage for the call so the check is deterministic across hosts rather
    than depending on the CI machine's own locale already being UTF-8 (which would mask the bug).
    """

    def test_command_output_decodes_as_utf8_regardless_of_host_locale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory))
            argv = [sys.executable, "-c", _UTF8_STDIO_SCRIPT]
            with unittest.mock.patch(
                "locale.getpreferredencoding", return_value="cp1251"
            ):
                record = run_command(run, verification_stage("VR-01", attempt=1),
                                     ".", argv, timeout=10)

        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["stdout"], "café — naïve")
        self.assertEqual(record["stderr"], "naïve — café")


if __name__ == "__main__":
    unittest.main()
