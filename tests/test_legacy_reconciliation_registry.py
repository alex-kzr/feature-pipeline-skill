"""TSL-14 — the project-declared legacy reconciliation registry."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from feature_pipeline.contracts import TaskSpec
from pipeline_core.execution import persist_task_contracts, reconcile_historical_cards
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.reconciliation_registry import (
    ReconciliationRegistryError,
    load_reconciliation_registry,
    reconciliation_supersession_graph,
)
from pipeline_core.state import ACTOR_RUNNER, Run
from pipeline_core.task_files import load_task_spec

_METADATA_TEMPLATE = """# {task_id} - {title}

## Execution Metadata
- Type: python
- Executor: python-executor
- Depends on: none
- Allowed scope: `src/**`
- Out of scope: none
- Required skills: none
- Maximum repair attempts: 2
- Documentation impact: none
- Verification commands:
  - `.` -> `git diff --check`
- Blocking conditions: none

## Acceptance Criteria
- [ ] AC-1 — The functional slice is complete.
"""


def _write_registry(root: Path, mappings: list[dict[str, str]] | object) -> Path:
    path = root / "tools" / "feature-pipeline" / "config" / "legacy_reconciliation_registry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = mappings if isinstance(mappings, dict) else {"schema_version": 1, "mappings": mappings}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_task_file(root: Path, task_id: str) -> Path:
    path = root / "docs" / "plans" / "tasks" / f"{task_id}_slug.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_METADATA_TEMPLATE.format(task_id=task_id, title=f"{task_id} title"), encoding="utf-8")
    return path


class LoadReconciliationRegistryTests(unittest.TestCase):
    def test_missing_registry_file_declares_no_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "does-not-exist.json"
            self.assertEqual(load_reconciliation_registry(missing), ())

    def test_valid_registry_parses_direct_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_registry(root, [
                {"historical_task": "TSL-03", "replacement_task": "TSL-05"},
                {"historical_task": "TSL-04", "replacement_task": "TSL-06"},
            ])
            edges = load_reconciliation_registry(path)
            self.assertEqual(
                {(edge.superseded, edge.replacement) for edge in edges},
                {("TSL-03", "TSL-05"), ("TSL-04", "TSL-06")},
            )

    def test_malformed_registry_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_registry(root, {"schema_version": 1, "mappings": "not-a-list"})
            with self.assertRaises(ReconciliationRegistryError) as ctx:
                load_reconciliation_registry(path)
            self.assertEqual(ctx.exception.code, "reconciliation-registry-malformed")

    def test_self_mapping_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_registry(root, [{"historical_task": "TSL-03", "replacement_task": "TSL-03"}])
            with self.assertRaises(ReconciliationRegistryError) as ctx:
                load_reconciliation_registry(path)
            self.assertEqual(ctx.exception.code, "reconciliation-registry-self")

    def test_duplicate_historical_mapping_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_registry(root, [
                {"historical_task": "TSL-03", "replacement_task": "TSL-05"},
                {"historical_task": "TSL-03", "replacement_task": "TSL-06"},
            ])
            with self.assertRaises(ReconciliationRegistryError) as ctx:
                load_reconciliation_registry(path)
            self.assertEqual(ctx.exception.code, "reconciliation-registry-duplicate")

    def test_mapping_may_name_one_explicit_source_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_registry(root, [
                {
                    "historical_task": "TSL-03",
                    "replacement_task": "TSL-05",
                    "source_run": "2026-09-11T09-36-16Z-tsl03-verifier-evidence-recovery",
                },
            ])
            edges = load_reconciliation_registry(path)
            self.assertEqual(len(edges), 1)
            self.assertEqual(
                edges[0].source_run, "2026-09-11T09-36-16Z-tsl03-verifier-evidence-recovery",
            )

    def test_mapping_with_no_source_run_declares_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_registry(root, [
                {"historical_task": "TSL-03", "replacement_task": "TSL-05"},
            ])
            edges = load_reconciliation_registry(path)
            self.assertIsNone(edges[0].source_run)

    def test_non_string_source_run_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_registry(root, [
                {"historical_task": "TSL-03", "replacement_task": "TSL-05", "source_run": 12},
            ])
            with self.assertRaises(ReconciliationRegistryError) as ctx:
                load_reconciliation_registry(path)
            self.assertEqual(ctx.exception.code, "reconciliation-registry-malformed")

    def test_blank_source_run_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_registry(root, [
                {"historical_task": "TSL-03", "replacement_task": "TSL-05", "source_run": "   "},
            ])
            with self.assertRaises(ReconciliationRegistryError) as ctx:
                load_reconciliation_registry(path)
            self.assertEqual(ctx.exception.code, "reconciliation-registry-malformed")

    def test_path_like_source_run_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_registry(root, [
                {
                    "historical_task": "TSL-03", "replacement_task": "TSL-05",
                    "source_run": "../escape",
                },
            ])
            with self.assertRaises(ReconciliationRegistryError) as ctx:
                load_reconciliation_registry(path)
            self.assertEqual(ctx.exception.code, "reconciliation-registry-malformed")


class ReconciliationSupersessionGraphTests(unittest.TestCase):
    def test_unknown_task_fails_closed_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_task_file(root, "TSL-05")
            _write_registry(root, [{"historical_task": "TSL-03", "replacement_task": "TSL-05"}])
            self.assertIsNone(reconciliation_supersession_graph(root))

    def test_cyclic_mapping_fails_closed_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_task_file(root, "TSL-03")
            _write_task_file(root, "TSL-05")
            _write_registry(root, [
                {"historical_task": "TSL-03", "replacement_task": "TSL-05"},
                {"historical_task": "TSL-05", "replacement_task": "TSL-03"},
            ])
            self.assertIsNone(reconciliation_supersession_graph(root))

    def test_valid_mapping_resolves_replacement_task_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_task_file(root, "TSL-03")
            _write_task_file(root, "TSL-05")
            _write_registry(root, [{"historical_task": "TSL-03", "replacement_task": "TSL-05"}])
            result = reconciliation_supersession_graph(root)
            self.assertIsNotNone(result)
            graph, definitions = result
            self.assertEqual(graph.replacement_for("TSL-03"), "TSL-05")
            self.assertIsInstance(definitions["TSL-05"], TaskSpec)
            self.assertIsInstance(definitions["TSL-03"], TaskSpec)

    def test_graph_edges_retain_the_declared_source_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_task_file(root, "TSL-03")
            _write_task_file(root, "TSL-05")
            _write_registry(root, [{
                "historical_task": "TSL-03", "replacement_task": "TSL-05",
                "source_run": "2026-09-11T09-36-16Z-tsl03-verifier-evidence-recovery",
            }])
            result = reconciliation_supersession_graph(root)
            self.assertIsNotNone(result)
            graph, _definitions = result
            edge = next(e for e in graph.edges if e.superseded == "TSL-03")
            self.assertEqual(
                edge.source_run, "2026-09-11T09-36-16Z-tsl03-verifier-evidence-recovery",
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


class RegistryDrivenReconciliationTests(unittest.TestCase):
    """A registry mapping retires a historical card whose task file declares no
    ``## Supersession`` section of its own — its own contract is never touched."""

    def test_reconciles_a_registry_mapped_historical_card_without_touching_its_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            historical = _write_task_file(root, "TSL-03")
            historical_bytes_before = historical.read_bytes()
            replacement = _write_task_file(root, "TSL-05")
            replacement_bytes_before = replacement.read_bytes()
            unfinished = _write_task_file(root, "TSL-10")
            unfinished_replacement = _write_task_file(root, "TSL-12")

            _write_registry(root, [
                {"historical_task": "TSL-03", "replacement_task": "TSL-05"},
                {"historical_task": "TSL-10", "replacement_task": "TSL-12"},
            ])

            board = root / "board.md"
            board.write_text(
                "## To Do\n"
                "- [TSL-03: stale historical](docs/plans/tasks/TSL-03_slug.md)\n"
                "- [TSL-10: unfinished historical](docs/plans/tasks/TSL-10_slug.md)\n"
                "- [OTHER-01: unrelated](docs/plans/tasks/OTHER-01_slug.md)\n\n"
                "## In Progress\n",
                encoding="utf-8",
            )

            # Evidence must key off the *real* replacement task file so its persisted
            # ``task_path``/contract digest match what the registry graph resolves — never a
            # synthetic stand-in.
            replacement_spec = load_task_spec(replacement)
            unfinished_replacement_spec = load_task_spec(unfinished_replacement)
            source = Run.create(
                "historical", root / "prompt.md", None, root / ".pipeline/runs/historical", root
            )
            RunLifecycle.initialize(source, tasks=[("TSL-05", ()), ("TSL-12", ())])
            persist_task_contracts(source, (replacement_spec, unfinished_replacement_spec))
            source.transition_task("TSL-05", "in_progress", actor=ACTOR_RUNNER)
            source.transition_task("TSL-12", "in_progress", actor=ACTOR_RUNNER)
            source.record_verdicts("TSL-05", "PASS", "PASS")
            source.record_verdicts("TSL-12", "FAIL", "PASS")
            source.status = "blocked"
            source.save()
            source_bytes = (source.run_dir / "run.json").read_bytes()

            current = Run.create(
                "reconciliation", root / "prompt.md", None,
                root / ".pipeline/runs/reconciliation", root,
            )
            audit = _spec("AUD-01")
            life = RunLifecycle.initialize(current, tasks=[("AUD-01", ())])

            removed = reconcile_historical_cards(life, board, {"AUD-01": audit})

            self.assertEqual(removed, ("TSL-03",))
            board_text = board.read_text(encoding="utf-8")
            self.assertNotIn("TSL-03", board_text)
            self.assertIn("TSL-10", board_text)
            self.assertIn("OTHER-01", board_text)
            self.assertEqual(historical.read_bytes(), historical_bytes_before)
            self.assertEqual(replacement.read_bytes(), replacement_bytes_before)
            self.assertEqual((source.run_dir / "run.json").read_bytes(), source_bytes)
            self.assertTrue(any(
                entry["scope"] == "board-reconciliation:historical"
                and entry["to"] == "TSL-03=TSL-05"
                for entry in current.history
            ))

    def test_invalid_registry_removes_no_cards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_task_file(root, "TSL-03")
            replacement = _write_task_file(root, "TSL-05")
            _write_registry(root, [
                {"historical_task": "TSL-03", "replacement_task": "TSL-05"},
                {"historical_task": "TSL-05", "replacement_task": "TSL-03"},
            ])

            board = root / "board.md"
            board.write_text(
                "## To Do\n"
                "- [TSL-03: stale historical](docs/plans/tasks/TSL-03_slug.md)\n\n"
                "## In Progress\n",
                encoding="utf-8",
            )

            replacement_spec = load_task_spec(replacement)
            source = Run.create(
                "historical", root / "prompt.md", None, root / ".pipeline/runs/historical", root
            )
            RunLifecycle.initialize(source, tasks=[("TSL-05", ())])
            persist_task_contracts(source, (replacement_spec,))
            source.transition_task("TSL-05", "in_progress", actor=ACTOR_RUNNER)
            source.record_verdicts("TSL-05", "PASS", "PASS")
            source.status = "verified"
            source.save()

            current = Run.create(
                "reconciliation", root / "prompt.md", None,
                root / ".pipeline/runs/reconciliation", root,
            )
            audit = _spec("AUD-01")
            life = RunLifecycle.initialize(current, tasks=[("AUD-01", ())])

            removed = reconcile_historical_cards(life, board, {"AUD-01": audit})

            self.assertEqual(removed, ())
            self.assertIn("TSL-03", board.read_text(encoding="utf-8"))


class SourceBoundRegistryReconciliationTests(unittest.TestCase):
    """A registry mapping binds an otherwise-ambiguous replacement to one named run."""

    def test_named_source_run_resolves_ambiguous_evidence_and_keeps_unrelated_cards(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_task_file(root, "TSL-03")
            replacement = _write_task_file(root, "TSL-05")
            _write_task_file(root, "TSL-10")
            unfinished_replacement = _write_task_file(root, "TSL-12")

            named_run_id = "2026-09-11T09-36-16Z-tsl03-verifier-evidence-recovery"
            _write_registry(root, [
                {
                    "historical_task": "TSL-03", "replacement_task": "TSL-05",
                    "source_run": named_run_id,
                },
                {"historical_task": "TSL-10", "replacement_task": "TSL-12"},
            ])

            board = root / "board.md"
            board.write_text(
                "## To Do\n"
                "- [TSL-03: stale historical](docs/plans/tasks/TSL-03_slug.md)\n"
                "- [TSL-10: unfinished historical](docs/plans/tasks/TSL-10_slug.md)\n"
                "- [OTHER-01: unrelated](docs/plans/tasks/OTHER-01_slug.md)\n\n"
                "## In Progress\n",
                encoding="utf-8",
            )

            replacement_spec = load_task_spec(replacement)
            unfinished_replacement_spec = load_task_spec(unfinished_replacement)

            # Two independently closed runs both carry exact PASS/PASS evidence for TSL-05 —
            # the default lookup is ambiguous between them.
            named_source = Run.create(
                named_run_id, root / "prompt.md", None,
                root / ".pipeline/runs" / named_run_id, root,
            )
            RunLifecycle.initialize(named_source, tasks=[("TSL-05", ())])
            persist_task_contracts(named_source, (replacement_spec,))
            named_source.transition_task("TSL-05", "in_progress", actor=ACTOR_RUNNER)
            named_source.record_verdicts("TSL-05", "PASS", "PASS")
            named_source.status = "verified"
            named_source.save()
            named_source_bytes = (named_source.run_dir / "run.json").read_bytes()

            other_source = Run.create(
                "other-tsl05-run", root / "prompt.md", None,
                root / ".pipeline/runs/other-tsl05-run", root,
            )
            RunLifecycle.initialize(other_source, tasks=[("TSL-05", ()), ("TSL-12", ())])
            persist_task_contracts(other_source, (replacement_spec, unfinished_replacement_spec))
            other_source.transition_task("TSL-05", "in_progress", actor=ACTOR_RUNNER)
            other_source.transition_task("TSL-12", "in_progress", actor=ACTOR_RUNNER)
            other_source.record_verdicts("TSL-05", "PASS", "PASS")
            other_source.record_verdicts("TSL-12", "FAIL", "PASS")
            other_source.status = "blocked"
            other_source.save()
            other_source_bytes = (other_source.run_dir / "run.json").read_bytes()

            current = Run.create(
                "reconciliation", root / "prompt.md", None,
                root / ".pipeline/runs/reconciliation", root,
            )
            audit = _spec("AUD-01")
            life = RunLifecycle.initialize(current, tasks=[("AUD-01", ())])

            removed = reconcile_historical_cards(life, board, {"AUD-01": audit})

            self.assertEqual(removed, ("TSL-03",))
            board_text = board.read_text(encoding="utf-8")
            self.assertNotIn("TSL-03", board_text)
            self.assertIn("TSL-10", board_text)
            self.assertIn("OTHER-01", board_text)
            self.assertEqual((named_source.run_dir / "run.json").read_bytes(), named_source_bytes)
            self.assertEqual((other_source.run_dir / "run.json").read_bytes(), other_source_bytes)
            self.assertTrue(any(
                entry["scope"] == "board-reconciliation:historical"
                and entry["to"] == "TSL-03=TSL-05"
                and named_run_id in entry.get("note", "")
                for entry in current.history
            ))

    def test_unknown_named_source_run_fails_closed_and_retains_the_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_task_file(root, "TSL-03")
            replacement = _write_task_file(root, "TSL-05")

            _write_registry(root, [
                {
                    "historical_task": "TSL-03", "replacement_task": "TSL-05",
                    "source_run": "does-not-exist-run",
                },
            ])

            board = root / "board.md"
            board.write_text(
                "## To Do\n"
                "- [TSL-03: stale historical](docs/plans/tasks/TSL-03_slug.md)\n\n"
                "## In Progress\n",
                encoding="utf-8",
            )

            replacement_spec = load_task_spec(replacement)
            source = Run.create(
                "historical", root / "prompt.md", None, root / ".pipeline/runs/historical", root
            )
            RunLifecycle.initialize(source, tasks=[("TSL-05", ())])
            persist_task_contracts(source, (replacement_spec,))
            source.transition_task("TSL-05", "in_progress", actor=ACTOR_RUNNER)
            source.record_verdicts("TSL-05", "PASS", "PASS")
            source.status = "verified"
            source.save()

            current = Run.create(
                "reconciliation", root / "prompt.md", None,
                root / ".pipeline/runs/reconciliation", root,
            )
            audit = _spec("AUD-01")
            life = RunLifecycle.initialize(current, tasks=[("AUD-01", ())])

            removed = reconcile_historical_cards(life, board, {"AUD-01": audit})

            self.assertEqual(removed, ())
            self.assertIn("TSL-03", board.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
