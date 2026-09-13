"""Evidence-bound reconciliation for active cards from historical runs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from feature_pipeline.contracts import TaskSpec
from feature_pipeline.application.work_items import activate_work_item, register_work_items
from pipeline_core.adapters import LaunchResult
from pipeline_core.execution import persist_task_contracts, reconcile_historical_cards
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.state import ACTOR_RUNNER, Run
from pipeline_core.verification import (
    VerificationEvidence,
    VerifierAnchors,
    VerifierLaunchers,
    orchestrate_verification,
)


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


class AmendedSourceRunEvidenceStaysUnchangedTests(unittest.TestCase):
    """TAM-01 AC-7: SIR-01 stays one card while its expanded revision finishes."""

    def test_reconciliation_of_another_run_leaves_an_amended_source_run_untouched(self) -> None:
        from pipeline_core.plan import AmendmentRevision

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _spec("SIR-01")
            _write_task(root, spec)
            audit = _spec("AUD-01")
            _write_task(root, audit)

            board = root / "board.md"
            board.write_text("## To Do\n\n## In Progress\n", encoding="utf-8")

            source = Run.create("sir-01-run", root / "prompt.md", None,
                                root / ".pipeline/runs/sir-01-run", root)
            RunLifecycle.initialize(source, tasks=[("SIR-01", ())])
            persist_task_contracts(source, (spec,))
            source.transition_task("SIR-01", "in_progress", actor=ACTOR_RUNNER)
            original_report = source.run_dir / "reports" / "SIR-01" / "baseline-1.md"
            original_report.parent.mkdir(parents=True, exist_ok=True)
            original_report.write_text("suite fixture failed outside scope\n", encoding="utf-8")
            original_report_bytes = original_report.read_bytes()
            source.apply_amendment(
                AmendmentRevision(
                    task_id="SIR-01", revision=1, prior_digest="sha256:a", new_digest="sha256:b",
                    changed_fields=("verification_commands",),
                    added_paths=("tests/lifecycle_fixture_extra.py",),
                    rationale="declared full suite exposed lifecycle fixtures out of scope",
                    approved_by="a-human", source_evidence="report:baseline-1",
                    created_at="2026-09-12T00:00:00Z", epoch=1,
                ),
                new_digest="sha256:b", new_digest_version="tam01-amendment-v1",
            )
            # The same active task ID now owns the expanded lifecycle-fixture scope and
            # completes a fresh independent verification epoch. The baseline report is
            # immutable historical evidence, not rewritten as a successful result.
            class PassingVerifier:
                def launch(self, request):  # noqa: ANN001
                    if request.resume_session_id:
                        return LaunchResult(0, json.dumps({
                            "role": request.role, "verdict": "PASS",
                            "task_id": request.task_id, "attempt": 1,
                        }), session_id="sir-verifier")
                    return LaunchResult(
                        0,
                        "- Verdict: PASS\n"
                        "- Findings: repaired lifecycle fixture passes\n"
                        "- Amendment-justification finding: revision 1, epoch 1: "
                        "declared full suite exposed lifecycle fixtures out of scope\n",
                        session_id="sir-verifier",
                    )

            register_work_items(source, (spec,))
            source.transition_task("SIR-01", "implemented", actor=ACTOR_RUNNER)
            with activate_work_item(source, "SIR-01"):
                outcome = orchestrate_verification(
                    source, spec,
                    VerificationEvidence(
                        "SIR-01", 1,
                        commands=({"id": "suite", "cwd": ".", "argv": ["true"], "exit_code": 0},),
                        amendment=source.task("SIR-01").amendment_revisions[-1],
                    ),
                    launchers=VerifierLaunchers(PassingVerifier(), PassingVerifier()),
                    anchors=VerifierAnchors(project_root=".", agents_root=".agents"),
                    attempt=1,
                )
            self.assertEqual(outcome.status, "done")
            source.save()

            current = Run.create("other-run", root / "prompt.md", None,
                                 root / ".pipeline/runs/other-run", root)
            life = RunLifecycle.initialize(current, tasks=[("AUD-01", ())])
            reconcile_historical_cards(life, board, {"SIR-01": spec, "AUD-01": audit})

            self.assertEqual(original_report.read_bytes(), original_report_bytes)
            reloaded = Run.load(source.run_dir, root)
            self.assertEqual(reloaded.task("SIR-01").current_revision, 1)
            self.assertEqual(len(reloaded.task("SIR-01").amendment_revisions), 1)
            self.assertEqual(reloaded.task("SIR-01").status, "done")
            self.assertEqual(reloaded.task("SIR-01").verification["task_verdict"], "PASS")
