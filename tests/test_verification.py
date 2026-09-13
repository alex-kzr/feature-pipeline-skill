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
import subprocess
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
from pipeline_core.commands import (
    VerificationRun,
    run_verification_commands,
    verification_stage,
)
from pipeline_core.plan import (
    AmendmentError,
    AmendmentRequiredResult,
    CLASSIFICATION_AMENDMENT_REQUIRED,
    CLASSIFICATION_ENVIRONMENTAL,
    CLASSIFICATION_TASK_ATTRIBUTABLE,
    classify_baseline_failure,
)
from pipeline_core.reports import build_verdict_envelope_prompt, verifier_artifacts
from pipeline_core.snapshot import SnapshotError
from pipeline_core.state import Run, StateError
from pipeline_core.verification import (
    VerificationError,
    VerificationEvidence,
    VerifierAnchors,
    VerifierLaunchers,
    _current_run_marker,
    build_verification_evidence,
    build_verifier_prompt,
    combine_verdict_status,
    current_run_mutation_reason,
    evidence_forces_fail,
    missing_command_evidence,
    orchestrate_verification,
)
from feature_pipeline.contracts import CommandSpec, TaskSpec
from feature_pipeline.application.work_items import activate_work_item, register_work_items

ANCHORS = VerifierAnchors(project_root="/repo", agents_root="/repo/.agents")


class FreshEnvelopePromptTests(unittest.TestCase):
    def test_fresh_continuation_receives_runner_observed_verdict(self) -> None:
        prompt = build_verdict_envelope_prompt(
            role="task_verifier", task_id="VR-02", attempt=1, observed_verdict="PASS"
        )

        self.assertIn("Runner-observed verdict from the verifier report: PASS.", prompt)


class ScopeAmendmentVerifierEvidenceTests(unittest.TestCase):
    def test_both_prompts_embed_the_same_amendment_and_require_a_justification_finding(self) -> None:
        evidence = VerificationEvidence(
            "VR-02",
            1,
            changed_files=(
                {
                    "path": "feature-pipeline-skill/src/feature_pipeline/domain/scope.py",
                    "status": "modified",
                    "digest": "sha256:amended",
                    "classification": "out_of_scope",
                },
            ),
        )
        payload = evidence.serialized()

        task_prompt = build_verifier_prompt(
            "task_verifier", _spec(), anchors=ANCHORS, feature_prompt="prompt.md",
            evidence_payload=payload, attempt=1,
        )
        test_prompt = build_verifier_prompt(
            "test_verifier", _spec(), anchors=ANCHORS, feature_prompt="prompt.md",
            evidence_payload=payload, attempt=1,
        )

        amendment = json.loads(payload)["scope_amendment"]
        self.assertEqual(
            amendment["observed_paths"],
            ["feature-pipeline-skill/src/feature_pipeline/domain/scope.py"],
        )
        self.assertEqual(task_prompt.split("```json\n", 1)[1], test_prompt.split("```json\n", 1)[1])
        for prompt in (task_prompt, test_prompt):
            self.assertIn("Amendment-justification finding:", prompt)
            self.assertIn("observed paths", prompt)


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
    register_work_items(run, (spec,))
    with activate_work_item(run, spec.id):
        return orchestrate_verification(
            run, spec, _evidence(**kw.pop("evidence_kw", {})),
            launchers=VerifierLaunchers(task=task, test=test),
            anchors=ANCHORS, attempt=1,
        )


# --- transition table (written before the orchestration) ------------------------------------

_MATRIX = {
    ("PASS", "PASS"): "done",
    ("PASS", "FAIL"): "in_progress",
    ("FAIL", "PASS"): "in_progress",
    ("FAIL", "FAIL"): "in_progress",
    ("PASS", "BLOCKED"): "in_progress",
    ("BLOCKED", "PASS"): "in_progress",
    ("FAIL", "BLOCKED"): "in_progress",
    ("BLOCKED", "FAIL"): "in_progress",
    ("BLOCKED", "BLOCKED"): "in_progress",
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

    def test_only_pass_pass_verifies(self) -> None:
        self.assertEqual(combine_verdict_status("PASS", "PASS"), "done")
        for combo in (("PASS", "FAIL"), ("FAIL", "PASS"), ("FAIL", "FAIL")):
            self.assertEqual(combine_verdict_status(*combo), "in_progress")
        for combo in (("PASS", "BLOCKED"), ("BLOCKED", "FAIL")):
            self.assertEqual(combine_verdict_status(*combo), "in_progress")

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
            self.assertEqual(outcome.status, "done")
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
        self.assertEqual(outcome.status, "done", outcome.failure)
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

            self.assertEqual(outcome.status, "done")
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


class CurrentRunMutationEvidenceTests(unittest.TestCase):
    """UGA-15: no-mutation criteria must not inherit historical task evidence."""

    def _run_scoped_spec(self) -> TaskSpec:
        return _spec(acceptance_criteria=(
            "Run-scoped: must not create commits, tags, pushes, workflow changes, or remote mutations.",
        ))

    def test_current_run_marker_recognizes_all_run_scoped_wording(self) -> None:
        for criterion in (
            "This run must not mutate the remote.",
            "Current run must not mutate the remote.",
            "Run-scoped: must not mutate the remote.",
            "[CURRENT-RUN ONLY] must not mutate the remote.",
        ):
            self.assertEqual(_current_run_marker(criterion), " [CURRENT-RUN ONLY]")

    def test_historical_result_does_not_contaminate_an_empty_current_run_boundary(self) -> None:
        historical_result = "## Result\n- Created commit deadbeef and pushed it.\n"
        evidence = _evidence(
            implementation_manifest=None,
            implementation_diff=None,
            changed_files=(),
            external_actions=(),
        )
        prompt = build_verifier_prompt(
            "task_verifier", self._run_scoped_spec(), anchors=ANCHORS,
            feature_prompt="prompt.md", evidence_payload=evidence.serialized(), attempt=1,
        )

        self.assertNotIn(historical_result, evidence.serialized())
        self.assertIn("[CURRENT-RUN ONLY]", prompt)
        self.assertIn("Ignore historical ## Result sections", prompt)
        boundary = json.loads(evidence.serialized())["current_run_boundary"]
        self.assertIsNone(boundary["implementation"]["manifest"])
        self.assertEqual(boundary["external_actions"], [])

    def test_current_run_mutation_evidence_remains_inside_the_boundary(self) -> None:
        evidence = _evidence(
            changed_files=({"path": ".github/workflows/quality.yml", "status": "modified"},),
            external_actions=({"action": "push", "ref": "refs/heads/main"},),
        )
        prompt = build_verifier_prompt(
            "task_verifier", self._run_scoped_spec(), anchors=ANCHORS,
            feature_prompt="prompt.md", evidence_payload=evidence.serialized(), attempt=1,
        )

        boundary = json.loads(evidence.serialized())["current_run_boundary"]
        self.assertEqual(
            boundary["implementation"]["changed_files"][0]["path"],
            ".github/workflows/quality.yml",
        )
        self.assertEqual(boundary["external_actions"][0]["action"], "push")
        self.assertIn("workflow change, ruleset mutation, or recorded external action", prompt)

    def test_runner_captured_current_run_commit_fails_a_scoped_no_mutation_criterion(self) -> None:
        evidence = _evidence(external_actions=({"action": "commit", "after": "deadbeef"},))

        self.assertEqual(
            current_run_mutation_reason(self._run_scoped_spec(), evidence),
            "runner captured current-run external action: commit",
        )

        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            outcome = _orchestrate(
                run, self._run_scoped_spec(), FakeVerifier(), FakeVerifier(),
                evidence_kw={"external_actions": ({"action": "commit", "after": "deadbeef"},)},
            )

        self.assertEqual(outcome.status, "in_progress")
        self.assertEqual(outcome.task_verdict, "FAIL")
        self.assertEqual(outcome.test_verdict, "FAIL")
        self.assertEqual(
            outcome.forced_fail_reason,
            "runner captured current-run external action: commit",
        )


class VerifierAttributionRulesTests(unittest.TestCase):
    def test_evidence_carries_prior_runner_projection_owners(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _implemented_run(root)
            run.add_task("TC-01")
            run.add_task("TC-02")
            board = root / "docs" / "kanban.md"
            tc01 = root / "docs" / "plans" / "tasks" / "TC-01.md"
            tc02 = root / "docs" / "plans" / "tasks" / "TC-02.md"
            for path in (board, tc01, tc02):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("runner projection\n", encoding="utf-8")
            run.record_runner_projection("TC-01", (board, tc01))
            run.record_runner_projection("TC-02", (tc02,))

            evidence = build_verification_evidence(
                run, "VR-02", attempt=1, commands_run=VerificationRun(())
            )

        self.assertEqual(
            [(row["task_id"], row["path"]) for row in evidence.runner_owned_writes],
            [
                ("TC-01", "docs/kanban.md"),
                ("TC-01", "docs/plans/tasks/TC-01.md"),
                ("TC-02", "docs/plans/tasks/TC-02.md"),
            ],
        )

    def test_task_verifier_uses_task_snapshot_and_manifest_not_ambient_git_diff(self) -> None:
        prompt = build_verifier_prompt(
            "task_verifier", _spec(), anchors=ANCHORS, feature_prompt="prompt.md",
            evidence_payload=_evidence().serialized(), attempt=1,
        )

        self.assertIn("task-relevant snapshot", prompt)
        self.assertIn("not whole-worktree git diff", prompt)
        self.assertIn("supplementary allowed-scope", prompt)


class ProseEnvelopeSettlementTests(unittest.TestCase):
    def test_amended_revision_requires_both_verifiers_to_assess_its_identity_and_rationale(self) -> None:
        amendment = {"revision": 1, "epoch": 1, "rationale": "suite fixture is outside scope"}
        prose = (
            "# verifier\n\n- Verdict: PASS\n\n- Findings: none\n"
            "- Amendment-justification finding: revision 1, epoch 1: suite fixture is outside scope\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            outcome = _orchestrate(
                run, _spec(), FakeVerifier(prose=prose), FakeVerifier(prose=prose),
                evidence_kw={"amendment": amendment},
            )
        self.assertEqual(outcome.status, "done")

    def test_amended_revision_cannot_settle_pass_without_the_required_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            outcome = _orchestrate(
                run, _spec(), FakeVerifier(), FakeVerifier(),
                evidence_kw={"amendment": {"revision": 1, "rationale": "outside scope"}},
            )
        self.assertIn("missing-amendment-justification-finding", outcome.failure or "")
        self.assertNotEqual(outcome.status, "done")

    def test_amended_revision_cannot_settle_pass_with_another_revision_identity(self) -> None:
        prose = (
            "# verifier\n\n- Verdict: PASS\n\n- Findings: none\n"
            "- Amendment-justification finding: revision 2, epoch 1: outside scope\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            outcome = _orchestrate(
                run, _spec(), FakeVerifier(prose=prose), FakeVerifier(prose=prose),
                evidence_kw={"amendment": {"revision": 1, "epoch": 1, "rationale": "outside scope"}},
            )
        self.assertIn("amendment-justification-missing-revision-identity", outcome.failure or "")

    def test_agreeing_prose_and_envelope_leave_no_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            outcome = _orchestrate(run, _spec(), FakeVerifier(), FakeVerifier())
            self.assertEqual(outcome.status, "done")
            self.assertIsNone(outcome.task_drift)
            self.assertIsNone(outcome.test_drift)

    def test_missing_prose_line_falls_back_to_the_envelope_with_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(prose="# task_verifier\n\nno verdict line at all\n")
            outcome = _orchestrate(run, _spec(), task, FakeVerifier())
            self.assertEqual(outcome.status, "done")
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
            self.assertEqual(outcome.status, "in_progress")
            self.assertIn("verdict-envelope-mismatch", outcome.failure)
            self.assertTrue(outcome.diagnostic.exists())
            self.assertNotEqual(run.task("VR-02").status, "verified")

    def test_explicit_localized_verdict_conflicting_with_envelope_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(
                prose="# task_verifier\n\nВердикт: **FAIL**\n",
                envelope=json.dumps(
                    {"role": "task_verifier", "verdict": "PASS",
                     "task_id": "VR-02", "attempt": 1}),
            )
            outcome = _orchestrate(run, _spec(), task, FakeVerifier())
            self.assertEqual(outcome.status, "in_progress")
            self.assertIn("verdict-envelope-mismatch", outcome.failure)
            self.assertNotEqual(run.task("VR-02").status, "verified")

    def test_conflicting_explicit_verdicts_block_regardless_of_order(self) -> None:
        cases = (
            "- Verdict: PASS\n- Verdict: FAIL\n",
            "- Verdict: FAIL\n- Вердикт: PASS\n",
            "- Вердикт: **BLOCKED**\n- Verdict: PASS\n",
        )
        for prose in cases:
            with self.subTest(prose=prose), tempfile.TemporaryDirectory() as directory:
                run = _implemented_run(Path(directory))
                task = FakeVerifier(
                    prose=prose,
                    envelope=json.dumps(
                        {"role": "task_verifier", "verdict": "PASS",
                         "task_id": "VR-02", "attempt": 1}),
                )

                outcome = _orchestrate(run, _spec(), task, FakeVerifier())

                self.assertEqual(outcome.status, "in_progress")
                self.assertIn("verdict-envelope-mismatch", outcome.failure)
                self.assertNotEqual(run.task("VR-02").status, "verified")

    def test_malformed_envelope_blocks_with_a_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(envelope="not json at all")
            outcome = _orchestrate(run, _spec(), task, FakeVerifier())
            self.assertEqual(outcome.status, "in_progress")
            self.assertIn("unparseable-verdict-envelope", outcome.failure)
            self.assertTrue(outcome.diagnostic.exists())


class LaunchFailureTests(unittest.TestCase):
    def test_a_nonzero_task_verifier_launch_blocks_and_skips_the_test_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(launch_exit=3)
            test = FakeVerifier()
            outcome = _orchestrate(run, _spec(), task, test)
            self.assertEqual(outcome.status, "in_progress")
            self.assertIn("exited with 3", outcome.failure)
            self.assertTrue(outcome.diagnostic.exists())
            self.assertEqual(test.calls, [])
            self.assertNotEqual(run.task("VR-02").status, "verified")

    def test_an_adapter_that_will_not_start_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            outcome = _orchestrate(
                run, _spec(), FakeVerifier(raise_code="adapter-unavailable"), FakeVerifier())
            self.assertEqual(outcome.status, "in_progress")
            self.assertIn("adapter-unavailable", outcome.failure)

    def test_a_missing_session_id_blocks_before_the_envelope_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(session_id=None)
            outcome = _orchestrate(run, _spec(), task, FakeVerifier())
            self.assertEqual(outcome.status, "in_progress")
            self.assertIn("no session id", outcome.failure)
            self.assertNotIn("envelope", [c["kind"] for c in task.calls])

    def test_a_nonzero_envelope_request_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            task = FakeVerifier(envelope_exit=1)
            outcome = _orchestrate(run, _spec(), task, FakeVerifier())
            self.assertEqual(outcome.status, "in_progress")
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

            spec = _spec()
            register_work_items(run, (spec,))
            with activate_work_item(run, spec.id):
                outcome = orchestrate_verification(
                    run, spec, evidence,
                    launchers=VerifierLaunchers(task=FakeVerifier(), test=FakeVerifier()),
                    anchors=ANCHORS, attempt=1)

            self.assertEqual(outcome.task_verdict, "PASS")
            self.assertEqual(outcome.test_verdict, "FAIL")
            self.assertEqual(outcome.status, "in_progress")
            self.assertIsNotNone(outcome.forced_fail_reason)
            self.assertNotEqual(run.task("VR-02").status, "verified")

    def test_guard_rejects_a_task_that_is_not_implemented(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _implemented_run(Path(directory))
            run.record_verdicts("VR-02", "PASS", "PASS")
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
            self.assertEqual(persisted["status"], "done")
            self.assertEqual(persisted["task_verdict"], "PASS")

            reloaded = Run.load(run.run_dir, run.repo_root)
            recorded = reloaded.task("VR-02").verification
            self.assertEqual(recorded["task_verdict"], "PASS")
            self.assertEqual(recorded["test_verdict"], "PASS")
            self.assertIsNotNone(recorded["verified_at"])
            self.assertEqual(reloaded.task("VR-02").status, "done")
            self.assertEqual(outcome.verdict_record, artifacts.verdict_record)


class IsolatedVerificationSnapshotTests(unittest.TestCase):
    """UEI-02 — the runner binds a task's verification evidence to an immutable identity of
    the tree the commands ran against, and refuses to verify against an unknown tree."""

    def _git(self, root: Path, *argv: str) -> None:
        subprocess.run(
            ["git", *argv], cwd=root, check=True, capture_output=True, text=True
        )

    def _repo_run(self, root: Path) -> Run:
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "t@example.com")
        self._git(root, "config", "user.name", "Test")
        (root / "pkg.py").write_text("source\n", encoding="utf-8")
        (root / "unrelated.py").write_text("other\n", encoding="utf-8")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-qm", "base")
        prompt = root / "prompt.md"
        prompt.write_text("feature prompt", encoding="utf-8")
        run = Run.create("verify", prompt, None, root / "runs" / "verify", root)
        run.add_task("VR-02")
        return run

    def _command(self) -> CommandSpec:
        return CommandSpec(".", (sys.executable, "-c", "print('ok')"))

    def test_every_command_record_carries_the_immutable_snapshot_identity(self) -> None:  # AC-1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._repo_run(root)
            stage = verification_stage("VR-02", attempt=1)

            result = run_verification_commands(
                run, (self._command(), self._command()), stage=stage,
                task_id="VR-02", attempt=1, allowed_scope=("pkg.py",))

            self.assertIsInstance(result, VerificationRun)
            token = result.snapshot["token"]
            self.assertTrue(token.startswith("snapshot:"))
            self.assertEqual(
                [record["snapshot"] for record in result.records], [token, token])

    def test_evidence_carries_the_snapshot_and_survives_an_unrelated_edit(self) -> None:  # AC-2
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._repo_run(root)
            stage = verification_stage("VR-02", attempt=1)
            result = run_verification_commands(
                run, (self._command(),), stage=stage, task_id="VR-02", attempt=1,
                allowed_scope=("pkg.py",))

            evidence = build_verification_evidence(
                run, "VR-02", attempt=1, commands_run=result)
            self.assertEqual(evidence.snapshot["token"], result.snapshot["token"])
            self.assertIn(result.snapshot["token"], evidence.serialized())
            self.assertEqual(
                evidence.as_dict()["snapshot"]["token"], result.snapshot["token"])

            before = evidence.serialized()
            # An unrelated worktree edit after the fact cannot move the recorded evidence.
            (root / "unrelated.py").write_text("changed later\n", encoding="utf-8")
            (root / "brand-new.py").write_text("noise\n", encoding="utf-8")
            self.assertEqual(evidence.serialized(), before)
            self.assertEqual(
                build_verification_evidence(
                    run, "VR-02", attempt=1, commands_run=result).serialized(),
                before,
            )

    def test_verification_fails_closed_when_no_snapshot_can_be_recorded(self) -> None:  # AC-3
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)  # not a Git work tree
            prompt = root / "prompt.md"
            prompt.write_text("feature prompt", encoding="utf-8")
            run = Run.create("verify", prompt, None, root / "runs" / "verify", root)
            run.add_task("VR-02")
            stage = verification_stage("VR-02", attempt=1)

            with self.assertRaises(SnapshotError):
                run_verification_commands(
                    run, (self._command(),), stage=stage, task_id="VR-02", attempt=1,
                    allowed_scope=("pkg.py",))
            self.assertEqual(run.stage_command_ids(stage), [])

    def test_no_snapshot_is_recorded_when_not_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._repo_run(root)
            stage = verification_stage("VR-02", attempt=1)
            result = run_verification_commands(
                run, (self._command(),), stage=stage, task_id="VR-02", attempt=1)
            self.assertIsNone(result.snapshot)
            self.assertNotIn("snapshot", result.records[0])


class BaselineFailureClassificationTests(unittest.TestCase):
    """TAM-01 AC-4: a pre-dispatch baseline failure is task-attributable, amendment-required,
    or environmental — never silently folded into an implementation defect."""

    def test_environmental_when_the_commands_own_working_directory_is_unavailable(self) -> None:
        self.assertEqual(
            classify_baseline_failure(exit_code=1, cwd_paths_exist=False,
                                      path_covered_by_scope=True),
            CLASSIFICATION_ENVIRONMENTAL,
        )

    def test_amendment_required_when_the_causal_evidence_is_outside_declared_scope(self) -> None:
        self.assertEqual(
            classify_baseline_failure(exit_code=1, cwd_paths_exist=True,
                                      path_covered_by_scope=False),
            CLASSIFICATION_AMENDMENT_REQUIRED,
        )

    def test_task_attributable_when_the_failure_is_inside_declared_scope(self) -> None:
        self.assertEqual(
            classify_baseline_failure(exit_code=1, cwd_paths_exist=True,
                                      path_covered_by_scope=True),
            CLASSIFICATION_TASK_ATTRIBUTABLE,
        )

    def test_a_passing_command_has_nothing_to_classify(self) -> None:
        with self.assertRaises(AmendmentError):
            classify_baseline_failure(exit_code=0, cwd_paths_exist=True,
                                      path_covered_by_scope=True)

    def test_amendment_required_result_retains_the_active_task_card(self) -> None:
        result = AmendmentRequiredResult(
            task_id="SIR-01", observed_paths=("tests/lifecycle/fixture_a.py",),
            causal_commands=("cd . > uv run python -m unittest discover -s tests -t .",),
            reason="declared suite fails outside the task's current scope",
        )
        payload = result.as_dict()
        self.assertEqual(payload["outcome"], "AMENDMENT_REQUIRED")
        self.assertEqual(payload["task_id"], "SIR-01")
        self.assertTrue(payload["requires_human_decision"])
        self.assertIn("tests/lifecycle/fixture_a.py", payload["observed_paths"])


if __name__ == "__main__":
    unittest.main()
