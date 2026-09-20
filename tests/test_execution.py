"""VR-03 — ``run_task`` orchestration and the narrow, idempotent task-file blocker edit.

Complements ``tests/test_repair.py`` (the bounded-repair-loop behaviours). Here:

* the happy path — a first pass that verifies with no repair;
* the loop's entry reconciliation (fresh / resumed-implemented / illegal state);
* the task file's ``## Blockers`` section: created when absent, updated in place on a
  re-block, and never touching the ``## Status`` / ``## Acceptance Criteria`` checkboxes.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from pipeline_core.execution import (
    ExecutionError,
    TaskExecution,
    _apply_approved_amendments,
    run_task,
)
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.plan import AmendmentRequest, build_amendment_revision, canonical_amendment_fields
from pipeline_core.state import Run
from pipeline_core.task_files import render_blocker_entry, upsert_blockers_section
from feature_pipeline.contracts import TaskSpec

from tests.support.builders import build_execution, initialize_run, narrow_blocker_spec
from tests.support.fakes import ScriptedExecutor, StubVerifier

# ``_spec``/``_run``/``_execution`` are thin, file-scoped names kept for readability inside this
# module's tests; the actual construction lives in ``tests.support`` (QG-01), shared with
# ``tests/test_repair.py`` and imported unchanged by ``tests/test_concrete_tool_grant.py``.
_spec = narrow_blocker_spec
_run = initialize_run


def _execution(spec: TaskSpec, executor, task, test) -> TaskExecution:
    return build_execution(spec, executor, task, test)


class HappyPathTests(unittest.TestCase):
    def test_first_pass_verifies_with_no_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec()
            executor = ScriptedExecutor(("implemented",))
            task = StubVerifier(("PASS",))
            test = StubVerifier(("PASS",))

            result = run_task(life, _execution(spec, executor, task, test))

            self.assertTrue(result.ok)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.status, "done")
            self.assertEqual(result.attempts, 0)
            self.assertEqual(result.gates, 1)
            self.assertEqual(result.blocker, None)
            self.assertEqual(len(result.passes), 1)
            self.assertEqual(result.passes[0].task_verdict, "PASS")
            self.assertEqual(life.run.task("VR-03").status, "done")
            self.assertEqual(executor.launches, 1)

    def test_rec35_approved_contract_controls_executor_and_verifier_briefings(self) -> None:
        """REC-35: the actual dispatch path never falls back to task-card scope after approval."""
        class AmendmentVerifier(StubVerifier):
            def launch(self, request):  # noqa: ANN001 - test adapter protocol
                result = super().launch(request)
                if not request.resume_session_id:
                    Path(request.report_path).write_text(
                        f"# {request.role}\n\n- Verdict: PASS\n\n"
                        "- Amendment-justification finding: revision 1, epoch 1: approved\n",
                        encoding="utf-8",
                    )
                return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_card = root / "REC-35.md"
            task_card.write_text("# Historical task card\n\nAllowed scope: src/**\n", encoding="utf-8")
            life = _run(root, tasks=(("REC-35", ()),))
            original = _spec(
                task_id="REC-35", path="REC-35.md", allowed_scope=("src/**",),
                out_of_scope=("reviews/**",), max_repair_attempts=1,
            )
            revision = build_amendment_revision(
                AmendmentRequest(
                    task_id="REC-35", task_status="to_do",
                    prior_contract=canonical_amendment_fields(original),
                    new_contract={
                        "allowed_scope": ["reviews/**", "src/**"],
                        "out_of_scope": [],
                        "verification_commands": [],
                        "max_repair_attempts": 3,
                        "documentation_impact": [],
                    },
                    rationale="repair needs the review artifact", approved_by="reviewer",
                    source_evidence="baseline:REC-35",
                ),
                next_revision=1, next_epoch=1,
            )
            life.run.apply_amendment(
                revision, new_digest=revision.new_digest,
                new_digest_version="tam01-amendment-v1",
            )
            effective = _apply_approved_amendments(life.run, (original,))[0]
            executor = ScriptedExecutor(("implemented",))
            task_verifier = AmendmentVerifier(("PASS",))
            test_verifier = AmendmentVerifier(("PASS",))

            result = run_task(life, _execution(effective, executor, task_verifier, test_verifier))

            self.assertTrue(result.ok)
            for prompt in (
                executor.calls[0]["prompt"],
                task_verifier.calls[0]["prompt"],
                test_verifier.calls[0]["prompt"],
            ):
                self.assertIn("- Allowed scope: reviews/**, src/**", prompt)
                self.assertIn("- Maximum repair attempts: 3", prompt)
                self.assertIn("runner-authoritative", prompt.casefold())
                self.assertTrue(
                    "historical context" in prompt or "cannot override" in prompt
                    or "overrides conflicting" in prompt,
                    prompt,
                )

    def test_resumed_in_progress_task_starts_a_fresh_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec()
            # A failed verification is retained as operation history.  Resume starts a fresh
            # executor operation rather than restoring a legacy intermediate task state.
            run_task(
                life,
                _execution(spec, ScriptedExecutor(("implemented", "implemented")),
                           StubVerifier(("FAIL", "PASS")), StubVerifier(("PASS", "PASS"))),
            )
            reloaded = Run.load(life.run.run_dir, root)
            reloaded.task("VR-03").status = "in_progress"
            reloaded.save()

            executor = ScriptedExecutor(("implemented",))
            result = run_task(
                RunLifecycle(reloaded),
                _execution(spec, executor, StubVerifier(("PASS",)), StubVerifier(("PASS",))),
            )
            self.assertEqual(result.status, "done")
            self.assertEqual(executor.launches, 1)

    def test_resume_continues_interrupted_verification_without_reimplementing(self) -> None:
        """A verifier wait is an operation boundary, not a reason to repeat implementation."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec()
            first_executor = ScriptedExecutor(("implemented",))
            first = run_task(
                life,
                _execution(spec, first_executor, StubVerifier(("BLOCKED",)),
                           StubVerifier(("PASS",))),
            )
            self.assertEqual(first.status, "waiting")
            self.assertEqual(first_executor.launches, 1)

            resumed_executor = ScriptedExecutor(("implemented",))
            reloaded = Run.load(life.run.run_dir, root)
            resumed = run_task(
                RunLifecycle(reloaded),
                _execution(spec, resumed_executor, StubVerifier(("PASS",)),
                           StubVerifier(("PASS",))),
            )

            self.assertEqual(resumed.status, "done")
            self.assertEqual(resumed_executor.launches, 0)
            self.assertTrue(any(
                entry["outcome"] == "resumed"
                for entry in reloaded.task("VR-03").operation_history
            ))

    def test_an_illegal_entry_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            life.run.task("VR-03").status = "unknown"
            with self.assertRaises(ExecutionError) as ctx:
                run_task(
                    life,
                    _execution(_spec(), ScriptedExecutor(), StubVerifier(("PASS",)),
                               StubVerifier(("PASS",))),
                )
            self.assertEqual(ctx.exception.code, "unexpected-entry-state")


class TaskFileBlockerSectionTests(unittest.TestCase):
    TASK_BODY = (
        "# VR-03 - Implement the Bounded Repair Loop\n\n"
        "## Status\n- [ ] To Do\n- [ ] In Progress\n- [ ] Done\n\n"
        "## Acceptance Criteria\n- [ ] AC-1 - The loop is bounded.\n"
    )

    def test_limit_escalation_preserves_task_state_without_touching_checkboxes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_rel = "docs/plans/tasks/VR-03_bounded-repair-loop.md"
            task_file = root / task_rel
            task_file.parent.mkdir(parents=True, exist_ok=True)
            task_file.write_text(self.TASK_BODY, encoding="utf-8")

            life = _run(root)
            spec = _spec(path=task_rel, max_repair_attempts=0)
            result = run_task(
                life,
                _execution(spec, ScriptedExecutor(("implemented",)),
                           StubVerifier(("FAIL",)), StubVerifier(("PASS",))),
            )
            self.assertEqual(result.status, "escalated")
            self.assertEqual(life.run.task("VR-03").status, "in_progress")
            self.assertEqual(life.run.task("VR-03").operation_history[-1]["outcome"], "escalated")

            text = task_file.read_text(encoding="utf-8")
            self.assertNotIn("## Blockers", text)
            # the pre-existing sections are byte-for-byte intact.
            self.assertIn("## Status\n- [ ] To Do\n- [ ] In Progress\n- [ ] Done\n", text)
            self.assertIn("- [ ] AC-1 - The loop is bounded.", text)
            self.assertTrue(text.endswith("\n"))
            self.assertNotIn("\n \n", text)  # no trailing-whitespace lines


class UpsertBlockersSectionUnitTests(unittest.TestCase):
    def _file(self, root: Path, body: str) -> Path:
        path = root / "task.md"
        path.write_text(body, encoding="utf-8")
        return path

    def test_creates_the_section_when_absent_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._file(root, "# T\n\n## Status\n- [ ] To Do\n")

            self.assertTrue(
                upsert_blockers_section(path, "run-1", "blocked at the limit",
                                       fields={"Task": "VR-03"}))
            first = path.read_text(encoding="utf-8")
            self.assertIn("## Blockers\n\n- [run-1] blocked at the limit\n  - Task: VR-03\n",
                          first)
            self.assertIn("## Status\n- [ ] To Do\n", first)

            # same key + same content -> no change.
            self.assertFalse(
                upsert_blockers_section(path, "run-1", "blocked at the limit",
                                       fields={"Task": "VR-03"}))
            self.assertEqual(path.read_text(encoding="utf-8"), first)

    def test_a_new_key_appends_and_an_existing_key_updates_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._file(root, "# T\n\n## Blockers\n\n- [run-1] first reason\n")

            self.assertTrue(upsert_blockers_section(path, "run-2", "second reason"))
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("- [run-"), 2)
            self.assertIn("- [run-1] first reason", text)
            self.assertIn("- [run-2] second reason", text)

            self.assertTrue(upsert_blockers_section(path, "run-1", "first reason (revised)"))
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("- [run-"), 2)  # still two entries
            self.assertIn("- [run-1] first reason (revised)", text)
            self.assertNotIn("first reason\n", text)

    def test_render_blocker_entry_shape(self) -> None:
        self.assertEqual(
            render_blocker_entry("k", "why", fields={"A": 1, "B": "x"}),
            ["- [k] why", "  - A: 1", "  - B: x"],
        )


if __name__ == "__main__":
    unittest.main()
