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

from pipeline_core.execution import ExecutionError, TaskExecution, run_task
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.state import Run
from pipeline_core.task_files import render_blocker_entry, upsert_blockers_section
from schemas.contracts import TaskSpec

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
            self.assertEqual(result.status, "verified")
            self.assertEqual(result.attempts, 0)
            self.assertEqual(result.gates, 1)
            self.assertEqual(result.blocker, None)
            self.assertEqual(len(result.passes), 1)
            self.assertEqual(result.passes[0].task_verdict, "PASS")
            self.assertEqual(life.run.task("VR-03").status, "verified")
            self.assertEqual(executor.launches, 1)

    def test_resumed_implemented_task_skips_straight_to_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            spec = _spec()
            # First pass implements then fails verification once, so the executor evidence is
            # already on the record; roll the record back to 'implemented' to model a resume
            # that lost only the verification window.
            run_task(
                life,
                _execution(spec, ScriptedExecutor(("implemented", "implemented")),
                           StubVerifier(("FAIL", "PASS")), StubVerifier(("PASS", "PASS"))),
            )
            reloaded = Run.load(life.run.run_dir, root)
            reloaded.task("VR-03").status = "implemented"
            reloaded.save()

            executor = ScriptedExecutor(("implemented",))
            result = run_task(
                RunLifecycle(reloaded),
                _execution(spec, executor, StubVerifier(("PASS",)), StubVerifier(("PASS",))),
            )
            self.assertEqual(result.status, "verified")
            self.assertEqual(executor.launches, 0)  # no executor dispatch on the resume

    def test_an_illegal_entry_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            life = _run(root)
            life.run.task("VR-03").status = "pending"
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

    def test_limit_block_appends_a_blockers_section_without_touching_checkboxes(self) -> None:
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
            self.assertEqual(result.status, "blocked")

            text = task_file.read_text(encoding="utf-8")
            self.assertIn("## Blockers", text)
            self.assertIn(f"- [{life.run.run_id}]", text)
            self.assertIn("maximum repair attempts", text)
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
