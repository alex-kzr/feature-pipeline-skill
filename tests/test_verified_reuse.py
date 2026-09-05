"""Task-contract fingerprints and read-only verified-evidence lookup."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from feature_pipeline.application.verified_reuse import (
    EvidenceEligibilityError,
    VerifiedEvidenceStore,
    canonical_task_contract,
    task_contract_digest,
)
from feature_pipeline.contracts import TaskSpec
from feature_pipeline.domain.models import MARKDOWN_TASK_FILE, TaskDefinition
from pipeline_core.execution import persist_task_contracts
from pipeline_core.state import Run


def _definition(
    *, path: str = "docs/plans/tasks/VR-01.md", criterion: str = "works",
    depends_on: tuple[str, ...] = ("VR-00",), allowed_scope: tuple[str, ...] = ("src/example.py",),
    out_of_scope: tuple[str, ...] = (".pipeline/runs/**",),
    command: tuple[str, ...] = ("uv", "run", "test"), tier: str = "full",
) -> TaskDefinition:
    return TaskDefinition(
        spec=TaskSpec.build(
            id="VR-01", title="Mutable title", path=path, task_type="python",
            executor="python-executor", depends_on=depends_on, allowed_scope=allowed_scope,
            out_of_scope=out_of_scope, verification_commands=({"cwd": ".", "argv": command},),
            verification_tier=tier, acceptance_criteria=({"id": "AC-1", "text": criterion},),
        ),
        source_format=MARKDOWN_TASK_FILE,
    )


def _source(root: Path, *, run_id: str, definition: TaskDefinition, status: str = "verified",
            task_verdict: str = "PASS", test_verdict: str = "PASS", digest: str | None = None) -> Path:
    path = root / "runs" / run_id / "run.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": 2, "feature": "source", "prompt_path": "other.md", "plan_path": "other.json",
        "run_id": run_id, "status": "verified", "tasks": [{
            "id": definition.id, "status": status, "task_path": definition.source_path,
            "task_contract_digest": task_contract_digest(definition) if digest is None else digest,
            "verification": {"task_verdict": task_verdict, "test_verdict": test_verdict,
                             "verified_at": "2026-09-05T09:00:00Z"},
        }],
    }, sort_keys=True), encoding="utf-8")
    return path


class TaskContractTests(unittest.TestCase):
    def test_contract_digest_uses_only_the_required_normalized_fields(self) -> None:
        first = _definition()
        equivalent = _definition(path="docs/plans/tasks/VR-01.md")
        self.assertEqual(task_contract_digest(first), task_contract_digest(equivalent))
        self.assertEqual(set(canonical_task_contract(first)), {
            "id", "depends_on", "allowed_scope", "out_of_scope", "acceptance_criteria",
            "verification_commands", "verification_tier",
        })
        changed = (
            _definition(depends_on=("VR-02",)), _definition(allowed_scope=("src/other.py",)),
            _definition(out_of_scope=("other/**",)), _definition(criterion="changed"),
            _definition(command=("uv", "run", "other")), _definition(tier="scoped"),
        )
        for definition in changed:
            with self.subTest(definition=definition):
                self.assertNotEqual(task_contract_digest(first), task_contract_digest(definition))


class VerifiedEvidenceStoreTests(unittest.TestCase):
    def test_exact_path_and_digest_match_returns_immutable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            source = _source(root, run_id="source-a", definition=definition)
            before = source.read_bytes()

            evidence = VerifiedEvidenceStore(root / "runs", root).find(definition)

            self.assertEqual(evidence["evidence_identity"], "task-path-and-contract-digest")
            self.assertEqual(evidence["source_run_digest"], "sha256:" + hashlib.sha256(before).hexdigest())
            self.assertEqual(evidence["dependency_id"], definition.id)
            with self.assertRaises(TypeError):
                evidence["source_run_id"] = "replacement"  # type: ignore[index]
            self.assertEqual(source.read_bytes(), before)

    def test_explicit_source_uses_the_same_eligibility_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            source = _source(root, run_id="selected", definition=definition)
            evidence = VerifiedEvidenceStore(root / "runs", root).find_at(source.parent, definition)
            self.assertEqual(evidence["source_run_id"], "selected")

    def test_explicit_legacy_source_rejects_another_eligible_legacy_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            selected = _source(root, run_id="selected", definition=definition)
            other = _source(root, run_id="other", definition=definition)
            for source in (selected, other):
                payload = json.loads(source.read_text())
                payload["tasks"][0].pop("task_path")
                payload["tasks"][0].pop("task_contract_digest")
                source.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(EvidenceEligibilityError) as ambiguous:
                VerifiedEvidenceStore(root / "runs", root).find_at(selected.parent, definition)

            self.assertEqual(ambiguous.exception.code, "evidence-legacy-ambiguous")

    def test_changed_contract_and_incomplete_verdict_fail_closed_with_specific_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            _source(root, run_id="changed", definition=definition, digest="sha256:changed")
            with self.assertRaises(EvidenceEligibilityError) as changed:
                VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(changed.exception.code, "evidence-contract-digest-mismatch")

            root = Path(directory) / "failed"
            _source(root, run_id="failed", definition=definition, test_verdict="FAIL")
            with self.assertRaises(EvidenceEligibilityError) as failed:
                VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(failed.exception.code, "evidence-test-verdict-not-pass")

    def test_one_eligible_digestless_match_is_legacy_and_two_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            _source(root, run_id="legacy", definition=definition, digest=None)
            payload = json.loads((root / "runs" / "legacy" / "run.json").read_text())
            payload["tasks"][0].pop("task_path")
            payload["tasks"][0].pop("task_contract_digest")
            (root / "runs" / "legacy" / "run.json").write_text(json.dumps(payload), encoding="utf-8")

            evidence = VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(evidence["evidence_identity"], "legacy-task-id")

            _source(root, run_id="legacy-two", definition=definition, digest=None)
            payload = json.loads((root / "runs" / "legacy-two" / "run.json").read_text())
            payload["tasks"][0].pop("task_path")
            payload["tasks"][0].pop("task_contract_digest")
            (root / "runs" / "legacy-two" / "run.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(EvidenceEligibilityError) as ambiguous:
                VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(ambiguous.exception.code, "evidence-legacy-ambiguous")


class ContractPersistenceTests(unittest.TestCase):
    def test_fresh_run_persists_each_task_path_and_contract_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = Run.create("evidence", root / "prompt.md", None, root / "runs" / "evidence", root)
            definition = _definition()
            run.add_task(definition.id)

            persist_task_contracts(run, (definition.spec,))

            task = run.task(definition.id)
            self.assertEqual(task.task_path, "docs/plans/tasks/VR-01.md")
            self.assertEqual(task.task_contract_digest, task_contract_digest(definition))


if __name__ == "__main__":
    unittest.main()
