"""VR-03 — the bounded, auditable repair loop.

Written before the loop existed. The cases cover the four properties the contract names:

* a repair limit of 0 / 1 / 2 spends at most that many attempts and then blocks once;
* every repair redispatches the *same* executor role in a fresh, unresumed context carrying
  the consolidated repair report, and reruns the complete verification gate;
* the consolidated repair report de-duplicates the finding lines and keeps product defects
  separate from environment problems;
* limit exhaustion writes one durable blocker, suppresses dependents transitively, preserves
  every earlier attempt's evidence, and returns a non-zero exit code. An external ``BLOCKED``
  outcome spends no attempt.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from pipeline_core.adapters import LaunchResult
from pipeline_core.execution import TaskExecution, run_task
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.prompt_envelope import EnvelopeAnchors
from pipeline_core.reports import (
    consolidate_findings,
    newest_repair_report,
    repair_report_path,
    write_repair_report,
)
from pipeline_core.state import ACTOR_RUNNER, Run
from pipeline_core.verification import VerifierAnchors, VerifierLaunchers
from schemas.contracts import TaskSpec

ENVELOPE_ANCHORS = EnvelopeAnchors(project_root=".", agents_root=".agents")
VERIFIER_ANCHORS = VerifierAnchors(project_root="/repo", agents_root="/repo/.agents")


# --- fixtures --------------------------------------------------------------------------------


def _spec(task_id: str = "VR-03", *, max_repair_attempts: int = 2, **overrides: object) -> TaskSpec:
    base: dict[str, object] = dict(
        id=task_id,
        title="Implement the bounded repair loop",
        path=f"docs/plans/tasks/{task_id}_bounded-repair-loop.md",
        task_type="python",
        executor="python-executor",
        allowed_scope=("feature-pipeline-skill/pipeline_core/execution.py",),
        out_of_scope=("feature-pipeline-skill/pipeline_core/runner_cli.py",),
        acceptance_criteria=(
            "Each FAIL consumes at most one repair attempt.",
            "Every repair returns through implemented and reruns the gate.",
        ),
        verification_commands=(),
        max_repair_attempts=max_repair_attempts,
    )
    base.update(overrides)
    return TaskSpec.build(**base)  # type: ignore[arg-type]


def _write(path: object, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class ScriptedExecutor:
    """A two-call executor adapter (prose report, then same-session status envelope).

    ``results`` gives the status the *n*-th executor launch settles to — ``"implemented"``,
    ``"blocked"``, or ``"launch-fail"`` for a non-zero launch. The status envelope echoes the
    ``attempt`` embedded in its own prompt, so a repair round (attempt 2, 3, …) settles too.
    """

    def __init__(self, results: tuple[str, ...] = ("implemented",)) -> None:
        self.results = list(results)
        self.launches = 0
        self.calls: list[dict] = []
        self._status = "implemented"

    def launch(self, request):  # noqa: ANN001 - test double
        prompt = request.prompt
        if request.no_tools or request.resume_session_id:
            match = re.search(r'"attempt":\s*(\d+)', prompt)
            attempt = int(match.group(1)) if match else 1
            text = json.dumps(
                {"role": "executor", "status": self._status,
                 "task_id": request.task_id, "attempt": attempt})
            _write(request.report_path, text)
            return LaunchResult(exit_code=0, stdout=text, session_id="exec-sess")

        index = self.launches
        self.launches += 1
        status = self.results[index] if index < len(self.results) else self.results[-1]
        self.calls.append(
            {
                "role": request.role,
                "fresh_session": request.fresh_session,
                "resume": request.resume_session_id,
                "prompt": prompt,
                "is_repair": "Repair of:" in prompt,
            }
        )
        if status == "launch-fail":
            return LaunchResult(exit_code=1, stdout="launch failed", session_id="exec-sess")
        self._status = "implemented" if status == "implemented" else "blocked"
        _write(
            request.report_path,
            f"# Executor report\n\n- Status: {self._status}\n\n## Files changed\n- none\n",
        )
        return LaunchResult(exit_code=0, stdout="", session_id="exec-sess")


class StubVerifier:
    """One verifier role. ``verdicts`` is the token this role returns per verification gate."""

    def __init__(self, verdicts: tuple[str, ...]) -> None:
        self.verdicts = list(verdicts)
        self.gate = -1
        self.calls: list[dict] = []

    def _verdict(self) -> str:
        return self.verdicts[min(self.gate, len(self.verdicts) - 1)]

    def launch(self, request):  # noqa: ANN001 - test double
        is_env = bool(request.resume_session_id)
        if not is_env:
            self.gate += 1
        verdict = self._verdict()
        self.calls.append(
            {
                "role": request.role,
                "is_env": is_env,
                "fresh_session": request.fresh_session,
                "read_only": request.read_only,
                "no_tools": request.no_tools,
                "prompt": request.prompt,
            }
        )
        if is_env:
            match = re.search(r'"attempt":\s*(\d+)', request.prompt)
            attempt = int(match.group(1)) if match else 1
            text = json.dumps(
                {"role": request.role, "verdict": verdict,
                 "task_id": request.task_id, "attempt": attempt})
        else:
            finding = "clean" if verdict == "PASS" else "defect: guard clause still missing"
            text = f"# {request.role}\n\n- Verdict: {verdict}\n\n- Findings: {finding}\n"
        _write(request.report_path, text)
        return LaunchResult(exit_code=0, stdout=text, session_id="ver-sess")


def _run(root: Path, *, tasks=(("VR-03", ()),)) -> RunLifecycle:
    prompt = root / "prompt.md"
    prompt.write_text("feature prompt", encoding="utf-8")
    run = Run.create("exec", prompt, None, root / "runs" / "exec", root)
    return RunLifecycle.initialize(run, tasks=list(tasks))


def _execution(
    spec: TaskSpec, executor: ScriptedExecutor, task: StubVerifier, test: StubVerifier
) -> TaskExecution:
    return TaskExecution(
        spec=spec,
        adapter=executor,
        launchers=VerifierLaunchers(task=task, test=test),
        verifier_anchors=VERIFIER_ANCHORS,
        envelope_anchors=ENVELOPE_ANCHORS,
        plan_path="docs/plans/2026-09-01-core-execution-engine.md",
    )


def _repair_reports(run: Run, task_id: str) -> list[int]:
    base = Path(run.run_dir) / "reports" / task_id
    return sorted(
        int(re.fullmatch(r"repair-([1-9][0-9]*)\.md", p.name).group(1))
        for p in base.glob("repair-*.md")
    )


# --- repair-limit boundary (0 / 1 / 2) ----------------------------------------------------


class RepairLimitBoundaryTests(unittest.TestCase):
    def test_limit_zero_one_two_spends_exactly_that_many_attempts(self) -> None:
        for maximum in (0, 1, 2):
            with self.subTest(maximum=maximum), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                life = _run(root)
                spec = _spec(max_repair_attempts=maximum)
                executor = ScriptedExecutor(("implemented",) * (maximum + 1))
                task = StubVerifier(("FAIL",) * (maximum + 1))
                test = StubVerifier(("PASS",) * (maximum + 1))

                result = run_task(life, _execution(spec, executor, task, test))

                self.assertEqual(result.status, "blocked")
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(result.attempts, maximum)
                self.assertEqual(result.gates, maximum + 1)
                # one executor dispatch per gate, one consolidated repair report per gate.
                self.assertEqual(executor.launches, maximum + 1)
                self.assertEqual(len(_repair_reports(life.run, "VR-03")), maximum + 1)
                self.assertIn("maximum repair attempts", result.blocker or "")
                self.assertIsNotNone(result.diagnostic)
                self.assertTrue(result.diagnostic.is_file())
                self.assertEqual(life.run.task("VR-03").status, "blocked")

    def test_a_successful_repair_stops_the_loop_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            executor = ScriptedExecutor(("implemented", "implemented"))
            task = StubVerifier(("FAIL", "PASS"))
            test = StubVerifier(("PASS", "PASS"))

            result = run_task(life, _execution(spec, executor, task, test))

            self.assertEqual(result.status, "verified")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.attempts, 1)
            self.assertEqual(result.gates, 2)
            self.assertEqual(executor.launches, 2)
            self.assertEqual(_repair_reports(life.run, "VR-03"), [2])
            self.assertEqual(life.run.task("VR-03").status, "verified")


# --- same-role fresh redispatch + full-gate rerun ---------------------------------------


class RedispatchAndGateRerunTests(unittest.TestCase):
    def test_repair_redispatches_the_same_role_fresh_with_the_repair_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=1)
            executor = ScriptedExecutor(("implemented", "implemented"))
            task = StubVerifier(("FAIL", "PASS"))
            test = StubVerifier(("PASS", "PASS"))

            run_task(life, _execution(spec, executor, task, test))

            first, repair = executor.calls
            self.assertFalse(first["is_repair"])
            self.assertEqual(repair["role"], first["role"])  # same executor role
            self.assertTrue(repair["fresh_session"])
            self.assertIsNone(repair["resume"])
            self.assertTrue(repair["is_repair"])
            self.assertIn("repair-2.md", repair["prompt"])
            self.assertIn("Preserve the original scope", repair["prompt"])

    def test_every_repair_reruns_the_full_verification_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            executor = ScriptedExecutor(("implemented",) * 3)
            task = StubVerifier(("FAIL", "FAIL", "PASS"))
            test = StubVerifier(("PASS", "PASS", "PASS"))

            result = run_task(life, _execution(spec, executor, task, test))

            self.assertEqual(result.status, "verified")
            self.assertEqual(result.gates, 3)
            # a fresh, independent verifier pair ran for every gate (3 initial launches each).
            self.assertEqual(sum(1 for c in task.calls if not c["is_env"]), 3)
            self.assertEqual(sum(1 for c in test.calls if not c["is_env"]), 3)
            for gate in (1, 2, 3):
                gate_dir = Path(life.run.run_dir) / "reports" / "VR-03" / f"verify-{gate}"
                self.assertTrue((gate_dir / f"task-verifier-{gate}.md").is_file())
                self.assertTrue((gate_dir / f"test-verifier-{gate}.md").is_file())
            for c in task.calls + test.calls:
                if not c["is_env"]:
                    self.assertTrue(c["read_only"])
                    self.assertTrue(c["fresh_session"])

    def test_declared_commands_are_rerun_once_per_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(
                max_repair_attempts=1,
                verification_commands=({"cwd": ".", "command": "python -c pass"},),
            )
            executor = ScriptedExecutor(("implemented", "implemented"))
            task = StubVerifier(("FAIL", "PASS"))
            test = StubVerifier(("PASS", "PASS"))

            run_task(life, _execution(spec, executor, task, test))

            self.assertIn("task:VR-03:verify:1", life.run.stages)
            self.assertIn("task:VR-03:verify:2", life.run.stages)


# --- external BLOCKED never spends an attempt ------------------------------------------


class ExternalBlockedTests(unittest.TestCase):
    def test_a_blocked_verdict_blocks_without_consuming_a_repair_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            executor = ScriptedExecutor(("implemented",))
            task = StubVerifier(("BLOCKED",))
            test = StubVerifier(("PASS",))

            result = run_task(life, _execution(spec, executor, task, test))

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.attempts, 0)
            self.assertEqual(executor.launches, 1)  # no repair redispatch
            self.assertEqual(_repair_reports(life.run, "VR-03"), [])
            self.assertEqual(life.run.task("VR-03").attempts, 0)

    def test_an_executor_that_cannot_implement_blocks_before_any_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            executor = ScriptedExecutor(("blocked",))
            task = StubVerifier(("PASS",))
            test = StubVerifier(("PASS",))

            result = run_task(life, _execution(spec, executor, task, test))

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.gates, 0)
            self.assertEqual(result.attempts, 0)
            self.assertEqual(task.calls, [])


# --- durable blocker + transitive dependent suppression ------------------------------


class BlockAtLimitTests(unittest.TestCase):
    def test_limit_block_persists_a_blocker_and_suppresses_dependents_transitively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root, tasks=(("AA-1", ()), ("BB-1", ("AA-1",)), ("CC-1", ("BB-1",))))
            spec = _spec("AA-1", max_repair_attempts=0)
            executor = ScriptedExecutor(("implemented",))
            task = StubVerifier(("FAIL",))
            test = StubVerifier(("PASS",))

            result = run_task(life, _execution(spec, executor, task, test))
            self.assertEqual(result.status, "blocked")

            reloaded = Run.load(life.run.run_dir, root)
            self.assertEqual(reloaded.task("AA-1").status, "blocked")
            self.assertTrue(reloaded.task("AA-1").blocker)
            self.assertTrue((reloaded.task("BB-1").blocker or "").startswith("blocked_by: "))
            self.assertTrue((reloaded.task("CC-1").blocker or "").startswith("blocked_by: "))

            packet_ref = reloaded.artifacts["blocker:AA-1"]
            packet = json.loads((root / packet_ref).read_text(encoding="utf-8"))
            self.assertEqual(packet["task_id"], "AA-1")
            self.assertEqual(packet["max_repair_attempts"], 0)

    def test_earlier_attempt_evidence_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)
            executor = ScriptedExecutor(("implemented",) * 3)
            task = StubVerifier(("FAIL", "FAIL", "FAIL"))
            test = StubVerifier(("PASS", "PASS", "PASS"))

            run_task(life, _execution(spec, executor, task, test))

            reports = Path(life.run.run_dir) / "reports" / "VR-03"
            for generation in (1, 2, 3):
                self.assertTrue((reports / f"launch-{generation}").is_dir())
            for gate in (1, 2, 3):
                self.assertTrue((reports / f"verify-{gate}").is_dir())
            self.assertEqual(_repair_reports(life.run, "VR-03"), [2, 3, 4])


# --- consolidated repair-report shape --------------------------------------------------


class RepairReportConsolidationTests(unittest.TestCase):
    def test_findings_are_deduplicated_across_both_verifier_reports(self) -> None:
        merged = consolidate_findings(
            "# task_verifier\n- Verdict: FAIL\n- missing guard clause\n- no test for empty input\n",
            "# test_verifier\n- Verdict: FAIL\n- missing guard clause\n- command exited 1\n",
        )
        self.assertEqual(
            merged,
            ("- Verdict: FAIL", "- missing guard clause", "- no test for empty input",
             "- command exited 1"),
        )

    def test_report_separates_product_defects_from_environment_problems(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec()
            report = write_repair_report(
                life.run, spec, 2,
                task_verifier_text="- Verdict: FAIL\n- AC-1 not met\n",
                test_verifier_text="- Verdict: FAIL\n- AC-1 not met\n- toolchain missing\n",
                source_attempt=1,
                product_defects=("AC-1 not met — add the guard clause",),
                environment_problems=("toolchain missing on the runner",),
                regression_tests=("feature-pipeline-skill -> uv run python -m unittest",),
            )
            text = report.path.read_text(encoding="utf-8")
            self.assertEqual(report.path, repair_report_path(life.run.run_dir, "VR-03", 2))
            self.assertIn("## Product defects to fix", text)
            self.assertIn("- AC-1 not met — add the guard clause", text)
            self.assertIn("## Environment problems", text)
            self.assertIn("- toolchain missing on the runner", text)
            self.assertIn("## Required regression tests", text)
            self.assertIn("Original scope (unchanged): "
                          "feature-pipeline-skill/pipeline_core/execution.py", text)
            self.assertIn("verify-1/task-verifier-1.md", text)
            # both source reports are still embedded verbatim.
            self.assertIn("## Task-verifier report (verbatim)", text)
            self.assertIn("## Test-verifier report (verbatim)", text)

    def test_newest_repair_report_picks_the_highest_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec()
            for attempt in (2, 3):
                write_repair_report(
                    life.run, spec, attempt,
                    task_verifier_text="- Verdict: FAIL\n",
                    test_verifier_text="- Verdict: FAIL\n",
                    source_attempt=attempt - 1,
                )
            self.assertEqual(
                newest_repair_report(life.run.run_dir, "VR-03"),
                repair_report_path(life.run.run_dir, "VR-03", 3),
            )
            self.assertIsNone(newest_repair_report(life.run.run_dir, "NOPE-9"))


# --- resume at a repair boundary ------------------------------------------------------


class ResumeAtRepairBoundaryTests(unittest.TestCase):
    def test_resume_from_verification_failed_continues_the_open_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec(max_repair_attempts=2)

            # One failed gate, then a crash simulated at the repair boundary: the consolidated
            # repair report and the incremented attempt are on disk, and RESUME_ROLLBACKS has
            # rolled the interrupted 'repairing' state back to 'verification_failed'.
            first_executor = ScriptedExecutor(("implemented", "implemented"))
            run_task(
                life,
                _execution(spec, first_executor,
                           StubVerifier(("FAIL", "PASS")), StubVerifier(("PASS", "PASS"))),
            )  # verifies on the repair; attempts == 1, repair-2.md persisted

            reloaded = Run.load(life.run.run_dir, root)
            reloaded.task("VR-03").status = "verification_failed"
            reloaded.save()

            executor = ScriptedExecutor(("implemented",))
            result = run_task(
                RunLifecycle(reloaded),
                TaskExecution(
                    spec=spec, adapter=executor,
                    launchers=VerifierLaunchers(
                        task=StubVerifier(("PASS",)), test=StubVerifier(("PASS",))),
                    verifier_anchors=VERIFIER_ANCHORS, envelope_anchors=ENVELOPE_ANCHORS,
                ),
            )

            self.assertEqual(result.status, "verified")
            # the resume continued the already-open repair — no further attempt was consumed.
            self.assertEqual(reloaded.task("VR-03").attempts, 1)
            self.assertTrue(executor.calls[0]["is_repair"])
            self.assertIn("repair-2.md", executor.calls[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
