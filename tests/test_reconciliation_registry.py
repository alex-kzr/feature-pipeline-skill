"""REC-23 — named-source legacy reconciliation projects historical completion."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline_core.execution import persist_task_contracts, reconcile_historical_cards
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.state import ACTOR_RUNNER, Run
from pipeline_core.task_files import load_task_spec


def _write_task(root: Path, task_id: str) -> Path:
    path = root / "docs" / "plans" / "tasks" / f"{task_id}_example.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {task_id} - {task_id} title\n\n"
        "## Status\n- [ ] To Do\n- [ ] In Progress\n- [ ] Done\n\n"
        "## Execution Metadata\n"
        "- Type: python\n- Executor: python-executor\n- Depends on: none\n"
        "- Allowed scope: `src/**`\n- Out of scope: none\n- Required skills: none\n"
        "- Maximum repair attempts: 1\n- Documentation impact: none\n"
        "- Verification commands:\n  - `.` -> `git diff --check`\n"
        "- Blocking conditions: none\n",
        encoding="utf-8",
    )
    return path


class NamedSourceReconciliationTests(unittest.TestCase):
    def test_named_pass_pass_replacement_projects_historical_done_without_mutating_historical_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            historical_path = _write_task(root, "REC-21")
            replacement_path = _write_task(root, "REC-22")
            audit_path = _write_task(root, "AUD-01")
            historical = load_task_spec(historical_path)
            replacement = load_task_spec(replacement_path)
            audit = load_task_spec(audit_path)
            registry = root / "tools" / "feature-pipeline" / "config" / "legacy_reconciliation_registry.json"
            registry.parent.mkdir(parents=True)
            registry.write_text(json.dumps({"mappings": [{
                "historical_task": "REC-21", "replacement_task": "REC-22",
                "source_run": "rec-22-verified", "project_completion": True,
            }]}), encoding="utf-8")
            board = root / "docs" / "kanban.md"
            board.write_text(
                "## To Do\n- [REC-21: REC-21 title](plans/tasks/REC-21_example.md)\n\n"
                "## In Progress\n", encoding="utf-8",
            )

            historical_run = Run.create("rec-21-exhausted", root / "prompt.md", None,
                                        root / ".pipeline" / "runs" / "rec-21-exhausted", root)
            historical_life = RunLifecycle.initialize(historical_run, tasks=[("REC-21", ())])
            historical_life.transition("REC-21", "running", actor=ACTOR_RUNNER)
            historical_life.block("REC-21", "repair budget exhausted")
            historical_run.status = "blocked"
            historical_run.save()
            historical_run_bytes = (historical_run.run_dir / "run.json").read_bytes()

            source = Run.create("rec-22-verified", root / "prompt.md", None,
                                root / ".pipeline" / "runs" / "rec-22-verified", root)
            RunLifecycle.initialize(source, tasks=[("REC-22", ())])
            persist_task_contracts(source, (replacement,))
            source.transition_task("REC-22", "in_progress", actor=ACTOR_RUNNER)
            source.record_verdicts("REC-22", "PASS", "PASS")
            source.status = "verified"
            source.save()

            current = Run.create("reconciliation", root / "prompt.md", None,
                                 root / ".pipeline" / "runs" / "reconciliation", root)
            life = RunLifecycle.initialize(current, tasks=[("AUD-01", ())])
            self.assertEqual(reconcile_historical_cards(life, board, {audit.id: audit}), ("REC-21",))

            self.assertNotIn("REC-21", board.read_text(encoding="utf-8"))
            rendered = historical_path.read_text(encoding="utf-8")
            self.assertIn("- [x] Done", rendered)
            self.assertEqual(rendered.count("## Result"), 1)
            self.assertIn(
                f"REC-21 completed from REC-22 run `{source.run_id}`", rendered
            )
            self.assertIn(".pipeline/runs/rec-22-verified/run.json", rendered)
            self.assertEqual((historical_run.run_dir / "run.json").read_bytes(), historical_run_bytes)

    def test_nonterminal_named_source_leaves_historical_markdown_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            historical_path = _write_task(root, "REC-21")
            replacement_path = _write_task(root, "REC-22")
            audit_path = _write_task(root, "AUD-01")
            replacement = load_task_spec(replacement_path)
            audit = load_task_spec(audit_path)
            registry = root / "tools" / "feature-pipeline" / "config" / "legacy_reconciliation_registry.json"
            registry.parent.mkdir(parents=True)
            registry.write_text(json.dumps({"mappings": [{
                "historical_task": "REC-21", "replacement_task": "REC-22",
                "source_run": "rec-22-running",
            }]}), encoding="utf-8")
            board = root / "docs" / "kanban.md"
            board.write_text(
                "## To Do\n- [REC-21: REC-21 title](plans/tasks/REC-21_example.md)\n\n"
                "## In Progress\n", encoding="utf-8",
            )
            source = Run.create("rec-22-running", root / "prompt.md", None,
                                root / ".pipeline" / "runs" / "rec-22-running", root)
            RunLifecycle.initialize(source, tasks=[("REC-22", ())])
            persist_task_contracts(source, (replacement,))
            source.transition_task("REC-22", "in_progress", actor=ACTOR_RUNNER)
            source.record_verdicts("REC-22", "PASS", "PASS")
            source.save()
            task_bytes = historical_path.read_bytes()
            board_bytes = board.read_bytes()

            current = Run.create("reconciliation", root / "prompt.md", None,
                                 root / ".pipeline" / "runs" / "reconciliation", root)
            life = RunLifecycle.initialize(current, tasks=[("AUD-01", ())])

            self.assertEqual(reconcile_historical_cards(life, board, {audit.id: audit}), ())
            self.assertEqual(historical_path.read_bytes(), task_bytes)
            self.assertEqual(board.read_bytes(), board_bytes)
