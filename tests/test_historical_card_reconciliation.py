"""Evidence-bound reconciliation for active cards from historical runs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from feature_pipeline.contracts import TaskSpec
from pipeline_core.execution import persist_task_contracts, reconcile_historical_cards
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.state import ACTOR_RUNNER, Run


def _spec(task_id: str) -> TaskSpec:
    return TaskSpec.build(
        id=task_id,
        title=f"{task_id} title",
        path=f"tasks/{task_id}.md",
        task_type="python",
        executor="python-executor",
        allowed_scope=("src/**",),
        acceptance_criteria=("The functional slice is complete.",),
        verification_commands=(),
        max_repair_attempts=1,
    )


def _write_task(root: Path, spec: TaskSpec, supersedes: str | None = None) -> None:
    declaration = "" if supersedes is None else f"\n## Supersession\n- Supersedes: {supersedes}\n"
    path = root / spec.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {spec.id} - {spec.title}\n{declaration}", encoding="utf-8")


class HistoricalCardReconciliationTests(unittest.TestCase):
    def test_does_not_retire_a_card_through_an_unverified_intermediate_replacement(self) -> None:
        """Each retirement needs PASS/PASS evidence for its direct formal replacement."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            historical = _spec("OLD-01")
            intermediate = _spec("REC-01")
            verified_successor = _spec("REC-02")
            audit = _spec("AUD-01")
            for spec, supersedes in (
                (historical, None),
                (intermediate, "OLD-01"),
                (verified_successor, "REC-01"),
                (audit, None),
            ):
                _write_task(root, spec, supersedes)

            board = root / "board.md"
            board.write_text(
                "## To Do\n- [OLD-01: active historical task](tasks/OLD-01.md)\n\n"
                "## In Progress\n",
                encoding="utf-8",
            )
            source = Run.create("historical", root / "prompt.md", None,
                                root / ".pipeline/runs/historical", root)
            RunLifecycle.initialize(source, tasks=[("REC-01", ()), ("REC-02", ())])
            persist_task_contracts(source, (intermediate, verified_successor))
            source.transition_task("REC-02", "in_progress", actor=ACTOR_RUNNER)
            source.record_verdicts("REC-02", "PASS", "PASS")
            source.status = "verified"
            source.save()

            current = Run.create("reconciliation", root / "prompt.md", None,
                                 root / ".pipeline/runs/reconciliation", root)
            life = RunLifecycle.initialize(current, tasks=[("AUD-01", ())])
            definitions = {spec.id: spec for spec in (
                historical, intermediate, verified_successor, audit,
            )}

            self.assertEqual(reconcile_historical_cards(life, board, definitions), ())
            self.assertIn("OLD-01", board.read_text(encoding="utf-8"))

    def test_reconciles_only_stale_cards_with_verified_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = _spec("OLD-01")
            replacement = _spec("NEW-01")
            unfinished = _spec("OLD-02")
            unfinished_replacement = _spec("NEW-02")
            audit = _spec("AUD-01")
            for spec, supersedes in (
                (stale, None),
                (replacement, "OLD-01"),
                (unfinished, None),
                (unfinished_replacement, "OLD-02"),
                (audit, None),
            ):
                _write_task(root, spec, supersedes)

            board = root / "board.md"
            board.write_text(
                "## To Do\n"
                "- [OLD-01: stale eligible](tasks/OLD-01.md)\n"
                "- [OLD-02: genuinely unfinished](tasks/OLD-02.md)\n"
                "- [OTHER-01: unrelated](tasks/OTHER-01.md)\n\n"
                "## In Progress\n",
                encoding="utf-8",
            )
            source = Run.create("historical", root / "prompt.md", None,
                                root / ".pipeline/runs/historical", root)
            source_life = RunLifecycle.initialize(source, tasks=[("NEW-01", ()), ("NEW-02", ())])
            persist_task_contracts(source, (replacement, unfinished_replacement))
            source.transition_task("NEW-01", "in_progress", actor=ACTOR_RUNNER)
            source.transition_task("NEW-02", "in_progress", actor=ACTOR_RUNNER)
            source.record_verdicts("NEW-01", "PASS", "PASS")
            source.record_verdicts("NEW-02", "FAIL", "PASS")
            source.status = "blocked"
            source.save()
            source_bytes = (source.run_dir / "run.json").read_bytes()

            current = Run.create("reconciliation", root / "prompt.md", None,
                                 root / ".pipeline/runs/reconciliation", root)
            life = RunLifecycle.initialize(current, tasks=[("AUD-01", ())])
            historical_task_bytes = (root / unfinished.path).read_bytes()
            definitions = {spec.id: spec for spec in (stale, replacement, unfinished,
                                                       unfinished_replacement, audit)}

            self.assertEqual(reconcile_historical_cards(life, board, definitions), ("OLD-01",))
            self.assertNotIn("OLD-01", board.read_text(encoding="utf-8"))
            self.assertIn("OLD-02", board.read_text(encoding="utf-8"))
            self.assertEqual((source.run_dir / "run.json").read_bytes(), source_bytes)
            self.assertEqual((root / unfinished.path).read_bytes(), historical_task_bytes)
            self.assertTrue(any(
                entry["scope"] == "board-reconciliation:historical"
                and entry["to"] == "OLD-01=NEW-01"
                for entry in current.history
            ))

            history = list(current.history)
            self.assertEqual(reconcile_historical_cards(life, board, definitions), ())
            self.assertEqual(current.history, history)
