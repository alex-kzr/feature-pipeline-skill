"""VR-02 — orchestrate two fresh, independent, read-only verifier contexts over one runner-owned
evidence payload, settle each strict JSON verdict envelope against its prose report, and let
*only* the parsed ``PASS``/``FAIL``/``BLOCKED`` combination move task state.

The tests cover the four properties the contract names:

* the full nine-cell verdict matrix and the documented transition for every combination;
* fresh-session, read-only, and tool-less enforcement, and that neither verifier's output can
  reach the other's prompt;
* prose/envelope agreement, envelope-only fallback with drift, prose/envelope disagreement,
  and malformed envelopes;
* launch-failure diagnostics and the structural guarantee that no failure path yields
  ``verified`` — including an unbacked executor claim forcing the test verdict to ``FAIL``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline_core.adapters import (
    AdapterError,
    ClaudeAdapter,
    LaunchResult,
    build_claude_argv,
)
from pipeline_core.reports import build_verdict_envelope_prompt, verifier_artifacts
from pipeline_core.state import Run, StateError
from pipeline_core.verification import (
    VerificationError,
    VerificationEvidence,
    VerifierAnchors,
    VerifierLaunchers,
    build_verifier_prompt,
    combine_verdict_status,
    evidence_forces_fail,
    missing_command_evidence,
    orchestrate_verification,
)
from schemas.contracts import TaskSpec

ANCHORS = VerifierAnchors(project_root="/repo", agents_root="/repo/.agents")


class FreshEnvelopePromptTests(unittest.TestCase):
    def test_fresh_continuation_receives_runner_observed_verdict(self) -> None:
        prompt = build_verdict_envelope_prompt(
            role="task_verifier", task_id="VR-02", attempt=1, observed_verdict="PASS"
        )

        self.assertIn("Runner-observed verdict from the verifier report: PASS.", prompt)


def _spec(**overrides: object) -> TaskSpec:
    base: dict[str, object] = dict(
        id="VR-02",
        title="Orchestrate independent task and test verification",
        path="docs/plans/tasks/VR-02_independent-verifier-orchestration.md",
        task_type="python",
        executor="python-executor",
        allowed_scope=("feature-pipeline-skill/pipeline_core/verification.py",),
        acceptance_criteria=(
            "Both verifier roles run in fresh independent read-only contexts.",
            "Only PASS + PASS produces verified.",
        ),
        verification_commands=(
            {"cwd": "feature-pipeline-skill", "command": "uv run python -m unittest"},
        ),
        max_repair_attempts=2,
    )
    base.update(overrides)
    return TaskSpec.build(**base)  # type: ignore[arg-type]


def _implemented_run(root: Path, *, task_id: str = "VR-02") -> Run:
    prompt = root / "prompt.md"
    prompt.write_text("feature prompt", encoding="utf-8")
    run = Run.create("verify", prompt, None, root / "runs" / "verify", root)
    run.add_task(task_id)
    for state in ("ready", "running", "implemented"):
        run.transition_task(task_id, state)
    return run


def _evidence(task_id: str = "VR-02", attempt: int = 1, **kw: object) -> VerificationEvidence:
    return VerificationEvidence(
        task_id, attempt, commands=({"id": "command-1", "cwd": ".", "argv": ["true"]},), **kw)


class FakeVerifier:
    """A deterministic single-role verifier adapter for the two-call verify shape.

    A launch carrying ``resume_session_id`` is the same-session verdict-envelope continuation;
    anything else is the fresh initial launch. Every launch writes its text to the report path
    the runner supplied, exactly as the real adapter/CLI would.
    """

    def __init__(
        self,
        *,
        verdict: str = "PASS",
        attempt: int = 1,
        prose: str | None = None,
        envelope: str | None = None,
        launch_exit: int = 0,
        envelope_exit: int = 0,
        session_id: str | None = "v-sess",
        raise_code: str | None = None,
        write: bool = True,
        marker: str = "",
    ) -> None:
        self.verdict = verdict
        self.attempt = attempt
        self.prose = prose
        self.envelope = envelope
        self.launch_exit = launch_exit
        self.envelope_exit = envelope_exit
        self.session_id = session_id
        self.raise_code = raise_code
        self.write = write
        self.marker = marker
        self.calls: list[dict] = []

    def launch(self, request):  # noqa: ANN001 - test double
        is_envelope = bool(request.resume_session_id)
        self.calls.append(
            {
                "role": request.role,
                "read_only": request.read_only,
                "fresh_session": request.fresh_session,
                "resume": request.resume_session_id,
                "no_tools": request.no_tools,
                "prompt": request.prompt,
                "kind": "envelope" if is_envelope else "initial",
            }
        )
        if self.raise_code and not is_envelope:
            raise AdapterError("fake verifier failure", self.raise_code)
        if is_envelope:
            text = self.envelope if self.envelope is not None else json.dumps(
                {
                    "role": request.role,
                    "verdict": self.verdict,
                    "task_id": request.task_id,
                    "attempt": self.attempt,
                }
            )
            exit_code = self.envelope_exit
        else:
            text = self.prose if self.prose is not None else (
                f"# {request.role}\n\n{self.marker}\n- Verdict: {self.verdict}\n\n"
                f"- Findings: none\n"
            )
            exit_code = self.launch_exit
        if self.write:
            path = Path(request.report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return LaunchResult(exit_code, text, session_id=self.session_id)


def _orchestrate(run: Run, spec: TaskSpec, task: FakeVerifier, test: FakeVerifier, **kw):
    return orchestrate_verification(
        run, spec, _evidence(**kw.pop("evidence_kw", {})),
        launchers=VerifierLaunchers(task=task, test=test),
        anchors=ANCHORS, attempt=1,
    )


# --- transition table (written before the orchestration) ------------------------------------

_MATRIX = {
    ("PASS", "PASS"): "verified",
    ("PASS", "FAIL"): "verification_failed",
    ("FAIL", "PASS"): "verification_failed",
    ("FAIL", "FAIL"): "verification_failed",
    ("PASS", "BLOCKED"): "blocked",
    ("BLOCKED", "PASS"): "blocked",
    ("FAIL", "BLOCKED"): "blocked",
    ("BLOCKED", "FAIL"): "blocked",
    ("BLOCKED", "BLOCKED"): "blocked",
}


class RecordVerdictsTransitionTableTests(unittest.TestCase):
    def test_every_combination_follows_the_documented_transition(self) -> None:
        for (task_verdict, test_verdict), expected in _MATRIX.items():
            with self.subTest(verdicts=(task_verdict, test_verdict)):
                with tempfile.TemporaryDirectory() as directory:
                    run = _implemented_run(Path(directory))
                    status = run.record_verdicts("VR-02", task_verdict, test_verdict)
                    self.assertEqual(status, expected)
                    self.assertEqual(run.task("VR-02").status, expected)
                    recorded = run.task("VR-02").verification
                    self.assertEqual(recorded["task_verdict"], task_verdict)
                    self.assertEqual(recorded["test_verdict"], test_verdict)
                    self.assertIsNotNone(recorded["verified_at"])
                    if expected == "blocked":
                        self.assertIn("BLOCKED", run.task("VR-02").blocker or "")

    def test_only_pass_pass_verifies(self) -> None:
        self.assertEqual(combine_verdict_status("PASS", "PASS"), "verified")
        for combo in (("PASS", "FAIL"), ("FAIL", "PASS"), ("FAIL", "FAIL")):
            self.assertEqual(combine_verdict_status(*combo), "verification_failed")
        for combo in (("PASS", "BLOCKED"), ("BLOCKED", "FAIL")):
            self.assertEqual(combine_verdict_status(*combo), "blocked")

    def test_an_unknown_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            with self.assertRaises(StateError):
                run.record_verdicts("VR-02", "PASS", "OK")


class VerdictMatrixOrchestrationTests(unittest.TestCase):
    def test_orchestration_produces_the_matrix_outcome_for_every_combination(self) -> None:
        for (task_verdict, test_verdict), expected in _MATRIX.items():
            with self.subTest(verdicts=(task_verdict, test_verdict)):
                with tempfile.TemporaryDirectory() as directory:
                    run = _implemented_run(Path(directory))
                    spec = _spec()
                    outcome = _orchestrate(
                        run, spec,
                        FakeVerifier(verdict=task_verdict),
                        FakeVerifier(verdict=test_verdict),
                    )
                    self.assertEqual(outcome.status, expected)
                    self.assertEqual(run.task("VR-02").status, expected)
                    self.assertEqual(outcome.task_verdict, task_verdict)
                    self.assertEqual(outcome.test_verdict, test_verdict)
                    self.assertIsNone(outcome.failure)

    def test_pass_pass_is_the_only_route_to_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            outcome = _orchestrate(run, _spec(), FakeVerifier(), FakeVerifier())
            self.assertEqual(outcome.status, "verified")
            self.assertIsNotNone(outcome.verdict_record)


class _AgentNameRecordingVerifier(FakeVerifier):
    """A :class:`FakeVerifier` that also keeps every :class:`LaunchRequest` it is handed, so a
    test can rebuild the production argv and inspect the ``--agent`` value that would reach the
    CLI (RDS-14)."""

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.requests: list = []

    def launch(self, request):  # noqa: ANN001 - test double
        self.requests.append(request)
        return super().launch(request)

    def initial_request(self):
        """The fresh verdict launch — not the same-session envelope continuation."""
        return next(r for r in self.requests if not r.resume_session_id)


class VerifierAgentNameRegressionTests(unittest.TestCase):
    """RDS-14: ``_run_one_verifier`` threads ``normalize_role``'s underscored form into
    ``LaunchRequest.role``; before the fix ``build_claude_argv`` put that on the wire verbatim as
    ``--agent task_verifier``, an agent name the CLI does not have (its on-disk agents are
    hyphenated). These drive the real ``orchestrate_verification`` path and assert the rebuilt
    argv carries the hyphenated on-disk name."""

    def _initial_requests(self, directory: str):
        run = _implemented_run(Path(directory))
        task = _AgentNameRecordingVerifier()
        test = _AgentNameRecordingVerifier()
        outcome = _orchestrate(run, _spec(), task, test)
        self.assertEqual(outcome.status, "verified", outcome.failure)
        return task.initial_request(), test.initial_request()

    def _agent_arg(self, request) -> str:  # noqa: ANN001 - test helper
        argv = build_claude_argv(request, executable="claude")
        return argv[argv.index("--agent") + 1]

    def test_task_verifier_launch_builds_the_hyphenated_on_disk_agent(self) -> None:  # AC-1
        with tempfile.TemporaryDirectory() as directory:
            task_request, _test_request = self._initial_requests(directory)
            self.assertEqual(self._agent_arg(task_request), "task-verifier")

    def test_test_verifier_launch_builds_the_hyphenated_on_disk_agent(self) -> None:  # AC-2
        with tempfile.TemporaryDirectory() as directory:
            _task_request, test_request = self._initial_requests(directory)
            self.assertEqual(self._agent_arg(test_request), "test-verifier")

    def test_the_underscored_normalized_form_never_reaches_the_agent_flag(self) -> None:  # AC-4
        with tempfile.TemporaryDirectory() as directory:
            for request in self._initial_requests(directory):
                self.assertNotIn("_", self._agent_arg(request))


_WRAPPED_VERIFIER_CLAUDE_FAKE = """\
import json, sys
sys.stdin.read()
argv = sys.argv[1:]
# The CLI's on-disk agent is hyphenated (task-verifier); a prompt-following model still emits
# normalize_role()'s underscored form in its envelope, which is what the runner settles against.
role = argv[argv.index("--agent") + 1].replace("-", "_")
if "--resume" in argv:
    result_text = json.dumps(
        {"role": role, "verdict": "PASS", "task_id": "VR-02", "attempt": 1}
    )
else:
    result_text = f"# {role}\\n\\n- Verdict: PASS\\n\\n- Findings: none\\n"
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": result_text, "session_id": "sess-verifier-wrapped",
    "usage": {}, "modelUsage": {}, "total_cost_usd": 0.02,
}))
"""


class VerifierResultTextExtractionTests(unittest.TestCase):
    """Regression for the same oxidium-forge failure shape on the verifier side (RDS-07):
    both verifier roles hit the identical strict-envelope-fed-the-raw-wrapper bug the moment a
    task gets past executor dispatch, per this task's evidence."""

    def test_wrapped_cli_output_settles_to_verified_not_unparseable_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "wrapped_verifier_claude.py"
            script.write_text(_WRAPPED_VERIFIER_CLAUDE_FAKE, encoding="utf-8")
            executable = [sys.executable, str(script)]

            run = _implemented_run(root)
            outcome = _orchestrate(
                run, _spec(),
                ClaudeAdapter(executable=executable),
                ClaudeAdapter(executable=executable),
            )

            self.assertEqual(outcome.status, "verified")
            self.assertEqual(outcome.task_verdict, "PASS")
            self.assertEqual(outcome.test_verdict, "PASS")
            self.assertIsNone(outcome.failure)


class FreshReadOnlyToollessTests(unittest.TestCase):
    def test_both_verifiers_launch_fresh_read_only_and_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(marker="TASK-ONLY-SECRET")
            test = FakeVerifier(marker="TEST-ONLY-SECRET")
            _orchestrate(run, _spec(), task, test)

            task_initial = next(c for c in task.calls if c["kind"] == "initial")
            test_initial = next(c for c in test.calls if c["kind"] == "initial")
            for call in (task_initial, test_initial):
                self.assertTrue(call["read_only"])
                self.assertTrue(call["fresh_session"])
                self.assertIsNone(call["resume"])
            # the task verifier may read; the test verifier is tool-less
            self.assertFalse(task_initial["no_tools"])
            self.assertTrue(test_initial["no_tools"])

            for envelope in (
                next(c for c in task.calls if c["kind"] == "envelope"),
                next(c for c in test.calls if c["kind"] == "envelope"),
            ):
                self.assertTrue(envelope["no_tools"])
                self.assertEqual(envelope["resume"], "v-sess")

            # no verifier's report text is ever fed into the other's prompt
            for call in test.calls:
                self.assertNotIn("TASK-ONLY-SECRET", call["prompt"])
            for call in task.calls:
                self.assertNotIn("TEST-ONLY-SECRET", call["prompt"])

    def test_both_prompts_embed_the_identical_evidence_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier()
            test = FakeVerifier()
            _orchestrate(run, _spec(), task, test)
            task_payload = task.calls[0]["prompt"].split("```json", 1)[1]
            test_payload = test.calls[0]["prompt"].split("```json", 1)[1]
            self.assertEqual(task_payload, test_payload)
            self.assertIn('"task_id":"VR-02"', task_payload)

    def test_test_verifier_prompt_forbids_running_checks(self) -> None:
        prompt = build_verifier_prompt(
            "test_verifier", _spec(), anchors=ANCHORS, feature_prompt="prompt.md",
            evidence_payload="{}", attempt=1)
        self.assertIn("no tools", prompt.lower())
        self.assertIn("no matching runner-recorded command is a FAIL", prompt)


class ProseEnvelopeSettlementTests(unittest.TestCase):
    def test_agreeing_prose_and_envelope_leave_no_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            outcome = _orchestrate(run, _spec(), FakeVerifier(), FakeVerifier())
            self.assertEqual(outcome.status, "verified")
            self.assertIsNone(outcome.task_drift)
            self.assertIsNone(outcome.test_drift)

    def test_missing_prose_line_falls_back_to_the_envelope_with_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(prose="# task_verifier\n\nno verdict line at all\n")
            outcome = _orchestrate(run, _spec(), task, FakeVerifier())
            self.assertEqual(outcome.status, "verified")
            self.assertIsNotNone(outcome.task_drift)

    def test_prose_envelope_disagreement_fails_closed_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(
                prose="# task_verifier\n\n- Verdict: PASS\n",
                envelope=json.dumps(
                    {"role": "task_verifier", "verdict": "FAIL",
                     "task_id": "VR-02", "attempt": 1}),
            )
            outcome = _orchestrate(run, _spec(), task, FakeVerifier())
            self.assertEqual(outcome.status, "blocked")
            self.assertIn("verdict-envelope-mismatch", outcome.failure)
            self.assertTrue(outcome.diagnostic.exists())
            self.assertNotEqual(run.task("VR-02").status, "verified")

    def test_malformed_envelope_blocks_with_a_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(envelope="not json at all")
            outcome = _orchestrate(run, _spec(), task, FakeVerifier())
            self.assertEqual(outcome.status, "blocked")
            self.assertIn("unparseable-verdict-envelope", outcome.failure)
            self.assertTrue(outcome.diagnostic.exists())


class LaunchFailureTests(unittest.TestCase):
    def test_a_nonzero_task_verifier_launch_blocks_and_skips_the_test_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(launch_exit=3)
            test = FakeVerifier()
            outcome = _orchestrate(run, _spec(), task, test)
            self.assertEqual(outcome.status, "blocked")
            self.assertIn("exited with 3", outcome.failure)
            self.assertTrue(outcome.diagnostic.exists())
            self.assertEqual(test.calls, [])
            self.assertNotEqual(run.task("VR-02").status, "verified")

    def test_an_adapter_that_will_not_start_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            outcome = _orchestrate(
                run, _spec(), FakeVerifier(raise_code="adapter-unavailable"), FakeVerifier())
            self.assertEqual(outcome.status, "blocked")
            self.assertIn("adapter-unavailable", outcome.failure)

    def test_a_missing_session_id_blocks_before_the_envelope_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(session_id=None)
            outcome = _orchestrate(run, _spec(), task, FakeVerifier())
            self.assertEqual(outcome.status, "blocked")
            self.assertIn("no session id", outcome.failure)
            self.assertNotIn("envelope", [c["kind"] for c in task.calls])

    def test_a_nonzero_envelope_request_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(envelope_exit=1)
            outcome = _orchestrate(run, _spec(), task, FakeVerifier())
            self.assertEqual(outcome.status, "blocked")
            self.assertIn("envelope request exited with 1", outcome.failure)


class NoFalseVerifiedTests(unittest.TestCase):
    def test_an_unbacked_executor_claim_forces_the_test_verdict_to_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            missing = missing_command_evidence(
                [{"cwd": ".", "argv": ["pytest", "-q", "never_run.py"]}], [])
            evidence = VerificationEvidence(
                "VR-02", 1,
                commands=({"id": "command-1", "cwd": ".", "argv": ["true"]},),
                missing_evidence=missing)
            self.assertIsNotNone(evidence_forces_fail(evidence))

            outcome = orchestrate_verification(
                run, _spec(), evidence,
                launchers=VerifierLaunchers(task=FakeVerifier(), test=FakeVerifier()),
                anchors=ANCHORS, attempt=1)

            self.assertEqual(outcome.task_verdict, "PASS")
            self.assertEqual(outcome.test_verdict, "FAIL")
            self.assertEqual(outcome.status, "verification_failed")
            self.assertIsNotNone(outcome.forced_fail_reason)
            self.assertNotEqual(run.task("VR-02").status, "verified")

    def test_guard_rejects_a_task_that_is_not_implemented(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            run.transition_task("VR-02", "verified")
            with self.assertRaises(VerificationError):
                _orchestrate(run, _spec(), FakeVerifier(), FakeVerifier())

    def test_guard_rejects_evidence_for_a_different_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            with self.assertRaises(VerificationError):
                orchestrate_verification(
                    run, _spec(), _evidence(attempt=2),
                    launchers=VerifierLaunchers(task=FakeVerifier(), test=FakeVerifier()),
                    anchors=ANCHORS, attempt=1)


class PersistenceTests(unittest.TestCase):
    def test_verifier_artifacts_and_verdicts_survive_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _implemented_run(root)
            outcome = _orchestrate(run, _spec(), FakeVerifier(), FakeVerifier())
            run.save()

            artifacts = verifier_artifacts(run.run_dir, "VR-02", 1)
            for path in (
                artifacts.task_prompt, artifacts.task_report, artifacts.task_envelope,
                artifacts.test_prompt, artifacts.test_report, artifacts.test_envelope,
                artifacts.verdict_record,
            ):
                self.assertTrue(path.exists(), path)
            persisted = json.loads(artifacts.verdict_record.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "verified")
            self.assertEqual(persisted["task_verdict"], "PASS")

            reloaded = Run.load(run.run_dir, run.repo_root)
            recorded = reloaded.task("VR-02").verification
            self.assertEqual(recorded["task_verdict"], "PASS")
            self.assertEqual(recorded["test_verdict"], "PASS")
            self.assertIsNotNone(recorded["verified_at"])
            self.assertEqual(reloaded.task("VR-02").status, "verified")
            self.assertEqual(outcome.verdict_record, artifacts.verdict_record)


if __name__ == "__main__":
    unittest.main()
