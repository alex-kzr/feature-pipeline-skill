"""Task-contract fingerprints and read-only verified-evidence lookup."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from feature_pipeline.application.verified_reuse import (
    CANONICAL_CONTRACT_VERSION,
    EvidenceEligibilityError,
    LEGACY_CANONICAL_CONTRACT_VERSION,
    REC01_RECOVERY_CONTRACT_VERSION,
    VerifiedEvidenceStore,
    canonical_task_contract,
    canonical_task_path,
    resolve_default_reuse,
    task_contract_digest,
)
from feature_pipeline.contracts import AcceptanceCriterionSpec, CommandSpec, Precondition, TaskSpec
from feature_pipeline.domain.models import MARKDOWN_TASK_FILE, TaskDefinition
from pipeline_core.plan import canonical_amendment_fields, contract_digest
from pipeline_core.execution import _next_actionable, _pending_reason, persist_task_contracts
from pipeline_core.lifecycle import RunLifecycle
from pipeline_core.reports import verifier_artifacts
from pipeline_core.state import ACTOR_HUMAN, Run
from pipeline_core.task_files import load_task_spec


def _definition(
    *, path: str = "docs/plans/tasks/VR-01.md", criterion: str = "works",
    depends_on: tuple[str, ...] = ("VR-00",), allowed_scope: tuple[str, ...] = ("src/example.py",),
    out_of_scope: tuple[str, ...] = (".pipeline/runs/**",),
    command: tuple[str, ...] = ("uv", "run", "test"), tier: str = "full",
    task_type: str = "python",
    required_skills: tuple[str, ...] = (), max_repair_attempts: int = 2,
    documentation_impact: tuple[str, ...] = (), accepts_scoped: tuple[str, ...] = (),
    deferred_command: tuple[str, ...] = (), runner_evidence: str | None = None,
    blocking_conditions: str | None = None,
    preconditions: tuple[dict[str, str], ...] = (),
) -> TaskDefinition:
    return TaskDefinition(
        spec=TaskSpec.build(
            id="VR-01", title="Mutable title", path=path, task_type=task_type,
            executor="python-executor", depends_on=depends_on, allowed_scope=allowed_scope,
            out_of_scope=out_of_scope, required_skills=required_skills,
            max_repair_attempts=max_repair_attempts, documentation_impact=documentation_impact,
            verification_commands=({"cwd": ".", "argv": command},), verification_tier=tier,
            accepts_scoped=accepts_scoped,
            deferred_verification_commands=(
                ({"cwd": ".", "argv": deferred_command},) if deferred_command else ()
            ),
            runner_evidence=runner_evidence, blocking_conditions=blocking_conditions,
            preconditions=preconditions,
            acceptance_criteria=({"id": "AC-1", "text": criterion},),
        ),
        source_format=MARKDOWN_TASK_FILE,
    )


def _source(root: Path, *, run_id: str, definition: TaskDefinition, status: str = "verified",
            task_verdict: str = "PASS", test_verdict: str = "PASS", digest: str | None = None,
            run_status: str = "verified", version: str | None = CANONICAL_CONTRACT_VERSION) -> Path:
    path = root / "runs" / run_id / "run.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": 2, "feature": "source", "prompt_path": "other.md", "plan_path": "other.json",
        "run_id": run_id, "status": run_status, "tasks": [{
            "id": definition.id, "status": status, "task_path": definition.source_path,
            "task_contract_digest": (
                task_contract_digest(definition, version=version)
                if digest is None and version is not None else digest
            ),
            "task_contract_version": version,
            "verification": {"task_verdict": task_verdict, "test_verdict": test_verdict,
                             "verified_at": "2026-09-05T09:00:00Z"},
        }],
    }, sort_keys=True), encoding="utf-8")
    return path


def _changed_definition(definition: TaskDefinition, **changes: object) -> TaskDefinition:
    return replace(definition, spec=replace(definition.spec, **changes))


class TaskContractTests(unittest.TestCase):
    def test_contract_digest_is_explicitly_versioned(self) -> None:
        definition = _definition()
        self.assertNotEqual(
            task_contract_digest(definition, version=LEGACY_CANONICAL_CONTRACT_VERSION),
            task_contract_digest(definition, version=CANONICAL_CONTRACT_VERSION),
        )

    def test_supersession_declaration_is_part_of_the_contract_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VR-01.md"
            path.write_text("# VR-01\n", encoding="utf-8")
            baseline = _definition(path=str(path))
            before = task_contract_digest(baseline)
            path.write_text(
                "# VR-01\n\n## Supersession\n- Supersedes: VR-00\n",
                encoding="utf-8",
            )
            self.assertNotEqual(before, task_contract_digest(baseline))

    def test_contract_digest_changes_when_execution_metadata_changes(self) -> None:
        """Reuse must reject an executor-routing change before an executor can launch."""
        self.assertNotEqual(
            task_contract_digest(_definition()),
            task_contract_digest(_definition(task_type="tooling")),
        )

    def test_contract_digest_uses_only_the_required_normalized_fields(self) -> None:
        first = _definition()
        equivalent = _definition(path="docs/plans/tasks/VR-01.md")
        self.assertEqual(task_contract_digest(first), task_contract_digest(equivalent))
        self.assertEqual(set(canonical_task_contract(first)), {
            "id", "task_type", "executor", "depends_on", "allowed_scope", "out_of_scope",
            "required_skills", "max_repair_attempts", "documentation_impact",
            "verification_commands", "verification_tier", "accepts_scoped",
            "deferred_verification_commands", "runner_evidence", "blocking_conditions",
            "preconditions", "acceptance_criteria", "supersedes",
        })
        changed = (
            _definition(depends_on=("VR-02",)), _definition(allowed_scope=("src/other.py",)),
            _definition(out_of_scope=("other/**",)), _definition(criterion="changed"),
            _definition(command=("uv", "run", "other")), _definition(tier="scoped"),
        )
        for definition in changed:
            with self.subTest(definition=definition):
                self.assertNotEqual(task_contract_digest(first), task_contract_digest(definition))

    def test_contract_digest_changes_for_every_execution_semantic_input(self) -> None:
        baseline = _definition(
            tier="scoped", depends_on=("VR-00", "VR-02"),
            required_skills=(".agents/skills/testing/SKILL.md",), max_repair_attempts=3,
            documentation_impact=("docs/agents/**",), accepts_scoped=("VR-00",),
            deferred_command=("uv", "run", "deferred"),
            runner_evidence="reverse-diff-and-restore", blocking_conditions="network",
            preconditions=({"kind": "approval", "value": "release"},),
        )
        changed = (
            _changed_definition(baseline, task_type="tooling"),
            _changed_definition(baseline, executor="other-executor"),
            _changed_definition(baseline, depends_on=("VR-00",)),
            _changed_definition(baseline, allowed_scope=("src/other.py",)),
            _changed_definition(baseline, out_of_scope=("other/**",)),
            _changed_definition(baseline, required_skills=()),
            _changed_definition(baseline, max_repair_attempts=2),
            _changed_definition(baseline, documentation_impact=()),
            _changed_definition(baseline, verification_commands=(CommandSpec(".", ("other",)),)),
            _changed_definition(baseline, verification_tier="full"),
            _changed_definition(baseline, accepts_scoped=("VR-02",)),
            _changed_definition(baseline, deferred_verification_commands=(CommandSpec(".", ("other",)),)),
            _changed_definition(baseline, runner_evidence=None),
            _changed_definition(baseline, blocking_conditions="other"),
            _changed_definition(baseline, preconditions=(Precondition("approval", "other"),)),
            _changed_definition(baseline, acceptance_criteria=(AcceptanceCriterionSpec("AC-1", "other"),)),
        )
        for definition in changed:
            with self.subTest(definition=definition):
                self.assertNotEqual(task_contract_digest(baseline), task_contract_digest(definition))


class VerifiedEvidenceStoreTests(unittest.TestCase):
    def test_rec09_v1_evidence_remains_readable_as_legacy_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            source = _source(
                root, run_id="rec09-v1-source", definition=definition,
                version=LEGACY_CANONICAL_CONTRACT_VERSION,
            )
            before = source.read_bytes()

            evidence = VerifiedEvidenceStore(root / "runs", root).find(definition)

            self.assertEqual(evidence["evidence_contract_version"], LEGACY_CANONICAL_CONTRACT_VERSION)
            self.assertEqual(evidence["evidence_identity"], "legacy-task-path-and-contract-digest")
            self.assertEqual(source.read_bytes(), before)

    def test_legacy_contract_is_rejected_when_canonical_identity_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            source = _source(
                root,
                run_id="rec05-source",
                definition=definition,
                digest=task_contract_digest(definition),
                version=None,
            )
            before = source.read_bytes()

            with self.assertRaises(EvidenceEligibilityError) as denied:
                VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(denied.exception.code, "evidence-canonical-identity-missing")
            self.assertEqual(source.read_bytes(), before)

    def test_legacy_digest_fails_closed_when_a_supersession_is_now_declared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_file = root / "tasks" / "VR-01.md"
            task_file.parent.mkdir()
            task_file.write_text("# VR-01\n", encoding="utf-8")
            definition = _definition(path=str(task_file))
            _source(
                root, run_id="rec05-source", definition=definition,
                digest=task_contract_digest(definition), version=None,
            )
            task_file.write_text(
                "# VR-01\n\n## Supersession\n- Supersedes: VR-00\n", encoding="utf-8"
            )

            with self.assertRaises(EvidenceEligibilityError) as denied:
                VerifiedEvidenceStore(root / "runs", root).find(definition)

            self.assertEqual(denied.exception.code, "evidence-canonical-identity-missing")

    def test_approved_amendment_contract_is_eligible_for_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            source = _source(root, run_id="amended-source", definition=definition)
            payload = json.loads(source.read_text(encoding="utf-8"))
            task = payload["tasks"][0]
            digest = contract_digest(canonical_amendment_fields(definition.spec))
            task.update({
                "task_contract_version": "tam01-amendment-v1",
                "task_contract_digest": digest,
                "current_revision": 1,
                "amendment_revisions": [{"revision": 1, "new_digest": digest}],
            })
            source.write_text(json.dumps(payload), encoding="utf-8")

            evidence = VerifiedEvidenceStore(root / "runs", root).find(definition.spec)

        self.assertEqual(evidence["source_run_id"], "amended-source")

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

    def test_explicit_legacy_source_is_rejected_even_when_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            selected = _source(root, run_id="selected", definition=definition)
            for source in (selected,):
                payload = json.loads(source.read_text())
                payload["tasks"][0].pop("task_path")
                payload["tasks"][0].pop("task_contract_digest")
                payload["tasks"][0].pop("task_contract_version")
                source.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(EvidenceEligibilityError) as denied:
                VerifiedEvidenceStore(root / "runs", root).find_at(selected.parent, definition)
            self.assertEqual(denied.exception.code, "evidence-canonical-identity-missing")

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

    def test_digestless_legacy_match_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            _source(root, run_id="legacy", definition=definition, digest=None)
            payload = json.loads((root / "runs" / "legacy" / "run.json").read_text())
            payload["tasks"][0].pop("task_path")
            payload["tasks"][0].pop("task_contract_digest")
            payload["tasks"][0].pop("task_contract_version")
            (root / "runs" / "legacy" / "run.json").write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(EvidenceEligibilityError) as denied:
                VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(denied.exception.code, "evidence-canonical-identity-missing")


class MarkdownDoneFinalArtifactTests(unittest.TestCase):
    """REC-19 AC-1: an attempt-bearing Markdown ``Done`` record must name evidence that is
    actually still on disk before it can be reused or reconciled — a Markdown Done/Result
    citing a run whose final verifier reports are absent is non-reconcilable, never verified
    evidence. A record with no durable attempt identity (no ``attempts`` count) keeps its
    established eligibility, since it cannot even name a deterministic final-artifact path."""

    @staticmethod
    def _done_task_file(root: Path) -> Path:
        task_file = root / "tasks" / "VR-01.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text(
            "# VR-01\n\n## Status\n- [ ] To Do\n- [ ] In Progress\n- [x] Done\n",
            encoding="utf-8",
        )
        return task_file

    def test_markdown_done_dependency_without_final_verifier_reports_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition(path=str(self._done_task_file(root)))
            source = _source(root, run_id="rec-19-source", definition=definition)
            payload = json.loads(source.read_text(encoding="utf-8"))
            payload["tasks"][0]["attempts"] = 0
            source.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(EvidenceEligibilityError) as denied:
                VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(denied.exception.code, "evidence-final-artifacts-missing")

    def test_markdown_done_dependency_with_final_verifier_reports_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition(path=str(self._done_task_file(root)))
            source = _source(root, run_id="rec-19-source", definition=definition)
            payload = json.loads(source.read_text(encoding="utf-8"))
            payload["tasks"][0]["attempts"] = 0
            payload["tasks"][0]["task_path"] = canonical_task_path(definition, root)
            source.write_text(json.dumps(payload), encoding="utf-8")
            artifacts = verifier_artifacts(source.parent, "VR-01", 1)
            artifacts.directory.mkdir(parents=True)
            artifacts.task_report.write_text("- Verdict: PASS\n", encoding="utf-8")
            artifacts.test_report.write_text("- Verdict: PASS\n", encoding="utf-8")

            evidence = VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(evidence["source_run_id"], "rec-19-source")

    def test_pre_attempt_legacy_markdown_done_record_keeps_existing_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition(path=str(self._done_task_file(root)))
            # No 'attempts' field is written: a pre-attempt legacy record with no durable
            # attempt identity, so it cannot name a deterministic final-artifact path.
            source = _source(root, run_id="rec-19-legacy-source", definition=definition)
            payload = json.loads(source.read_text(encoding="utf-8"))
            payload["tasks"][0]["task_path"] = canonical_task_path(definition, root)
            source.write_text(json.dumps(payload), encoding="utf-8")

            evidence = VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(evidence["source_run_id"], "rec-19-legacy-source")


class TerminalBlockedSourceReuseTests(unittest.TestCase):
    """A terminal ``blocked`` source run may still lend one fully-verified task's evidence."""

    def test_terminal_blocked_source_run_lends_a_fully_verified_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            source = _source(
                root, run_id="blocked-src", definition=definition, run_status="blocked")
            before = source.read_bytes()

            evidence = VerifiedEvidenceStore(root / "runs", root).find(definition)

            self.assertEqual(evidence["evidence_identity"], "task-path-and-contract-digest")
            self.assertEqual(evidence["task_verdict"], "PASS")
            self.assertEqual(evidence["test_verdict"], "PASS")
            self.assertEqual(source.read_bytes(), before)

    def test_explicit_terminal_blocked_source_run_uses_the_same_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            source = _source(
                root, run_id="selected", definition=definition, run_status="blocked")
            evidence = VerifiedEvidenceStore(root / "runs", root).find_at(source.parent, definition)
            self.assertEqual(evidence["source_run_id"], "selected")

    def test_blocked_source_run_with_an_unverified_task_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            _source(
                root, run_id="blocked-src", definition=definition,
                run_status="blocked", status="implemented")
            with self.assertRaises(EvidenceEligibilityError) as denied:
                VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(denied.exception.code, "evidence-source-task-not-verified")

    def test_blocked_source_run_with_a_failed_verdict_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            _source(
                root, run_id="blocked-src", definition=definition,
                run_status="blocked", test_verdict="FAIL")
            with self.assertRaises(EvidenceEligibilityError) as denied:
                VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(denied.exception.code, "evidence-test-verdict-not-pass")

    def test_blocked_source_run_with_a_mismatched_contract_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            _source(
                root, run_id="blocked-src", definition=definition,
                run_status="blocked", digest="sha256:changed")
            with self.assertRaises(EvidenceEligibilityError) as denied:
                VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(denied.exception.code, "evidence-contract-digest-mismatch")

    def test_non_terminal_source_run_is_still_rejected_as_not_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            _source(
                root, run_id="running-src", definition=definition, run_status="running")
            with self.assertRaises(EvidenceEligibilityError) as denied:
                VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(denied.exception.code, "evidence-source-run-not-closed")


class Rec01HistoricalEvidenceRecoveryTests(unittest.TestCase):
    """REC-18 preserves one exact REC-01 source without trusting active peers."""

    _REC01_PATH = "docs/plans/tasks/REC-01_executor-context-and-catalog-recovery.md"
    _REC01_HISTORICAL_DIGEST = (
        "sha256:e2bf7ced1852e5288638b2e76f28dcb190aa7447f2724514a7714581cf386e03"
    )

    @classmethod
    def _definition(cls) -> TaskDefinition:
        repository = Path(__file__).resolve().parents[2]
        return TaskDefinition(
            spec=load_task_spec(repository / cls._REC01_PATH),
            source_format=MARKDOWN_TASK_FILE,
        )

    @classmethod
    def _source(cls, root: Path, run_id: str, definition: TaskDefinition, *, run_status: str = "verified") -> Path:
        source = _source(
            root, run_id=run_id, definition=definition, run_status=run_status,
            digest=cls._REC01_HISTORICAL_DIGEST, version=None,
        )
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["tasks"][0]["task_path"] = cls._REC01_PATH
        source.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return source

    def test_closed_exact_rec01_source_is_selected_despite_nonterminal_rec01_peer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = self._definition()
            self._source(root, "active-rec01", definition, run_status="running")
            closed = self._source(root, "closed-rec01", definition)
            before = closed.read_bytes()

            evidence = VerifiedEvidenceStore(root / "runs", Path(__file__).resolve().parents[2]).find(definition)

            self.assertEqual(evidence["source_run_id"], "closed-rec01")
            self.assertEqual(evidence["evidence_contract_version"], REC01_RECOVERY_CONTRACT_VERSION)
            self.assertEqual(closed.read_bytes(), before)

    def test_multiple_closed_exact_rec01_sources_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = self._definition()
            for run_id in ("closed-rec01-a", "closed-rec01-b"):
                self._source(root, run_id, definition)

            with self.assertRaises(EvidenceEligibilityError) as denied:
                VerifiedEvidenceStore(root / "runs", Path(__file__).resolve().parents[2]).find(definition)

            self.assertEqual(denied.exception.code, "evidence-exact-ambiguous")


class SupersessionReuseDenialTests(unittest.TestCase):
    """REC-14 denial cases never grant default reuse to a terminal predecessor."""

    def _definitions(self, root: Path, *, extra: str = "") -> dict[str, TaskDefinition]:
        task_dir = root / "tasks"; task_dir.mkdir()
        paths = {name: task_dir / f"{name}.md" for name in ("TC-04", "REC-01", "TC-05")}
        paths["TC-04"].write_text("# TC-04\n", encoding="utf-8")
        paths["REC-01"].write_text(
            "# REC-01\n\n## Supersession\n- Supersedes: TC-04\n" + extra, encoding="utf-8")
        paths["TC-05"].write_text("# TC-05\n", encoding="utf-8")
        return {
            "TC-04": _changed_definition(_definition(path=str(paths["TC-04"])), id="TC-04"),
            "REC-01": _changed_definition(_definition(path=str(paths["REC-01"])), id="REC-01"),
            "TC-05": _changed_definition(_definition(path=str(paths["TC-05"]), depends_on=("TC-04",)), id="TC-05"),
        }

    def test_absent_stale_and_contract_incompatible_replacement_evidence_are_denied(self) -> None:
        for mode in ("absent", "stale", "incompatible"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); definitions = self._definitions(root)
                replacement = definitions["REC-01"]
                if mode == "stale":
                    _source(root, run_id="replacement", definition=replacement, status="implemented")
                elif mode == "incompatible":
                    _source(root, run_id="replacement", definition=replacement, digest="sha256:wrong")
                with self.assertRaises(EvidenceEligibilityError):
                    resolve_default_reuse(VerifiedEvidenceStore(root / "runs", root), definitions,
                        ["TC-04", "TC-05"], ["TC-05"], root)

    def test_ambiguous_and_cyclic_replacement_declarations_are_denied(self) -> None:
        for mode, extra in (
            ("ambiguous", "\n## Supersession\n- Supersedes: TC-04\n"),
            ("cyclic", ""),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); definitions = self._definitions(root)
                if mode == "ambiguous":
                    path = root / "tasks" / "REC-02.md"
                    path.write_text("# REC-02\n\n## Supersession\n- Supersedes: TC-04\n", encoding="utf-8")
                    definitions["REC-02"] = _changed_definition(_definition(path=str(path)), id="REC-02")
                else:
                    (root / "tasks" / "TC-04.md").write_text(
                        "# TC-04\n\n## Supersession\n- Supersedes: REC-01\n", encoding="utf-8")
                with self.assertRaises(EvidenceEligibilityError) as denied:
                    resolve_default_reuse(VerifiedEvidenceStore(root / "runs", root), definitions,
                        ["TC-04", "TC-05"], ["TC-05"], root)
                self.assertEqual(denied.exception.code, "evidence-supersession-invalid")


class CancelledResolutionReuseDenialTests(unittest.TestCase):
    """ROC-02 AC-3 — a cancelled prerequisite is not verified functionality and must never
    silently satisfy a functional dependency, whether read from persisted evidence or seen
    live in the same run."""

    def test_cancelled_done_task_is_not_reusable_verified_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            source = _source(root, run_id="cancelled-src", definition=definition, status="done")
            payload = json.loads(source.read_text())
            payload["tasks"][0]["resolution"] = "cancelled"
            payload["tasks"][0]["resolution_reason"] = "no longer needed"
            source.write_text(json.dumps(payload), encoding="utf-8")
            before = source.read_bytes()

            with self.assertRaises(EvidenceEligibilityError) as denied:
                VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(denied.exception.code, "evidence-source-task-not-verified")
            self.assertEqual(source.read_bytes(), before)  # never touched

    def test_completed_done_task_is_reusable_verified_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = _definition()
            source = _source(root, run_id="completed-src", definition=definition, status="done")
            payload = json.loads(source.read_text())
            payload["tasks"][0]["resolution"] = "completed"
            source.write_text(json.dumps(payload), encoding="utf-8")

            evidence = VerifiedEvidenceStore(root / "runs", root).find(definition)
            self.assertEqual(evidence["task_verdict"], "PASS")
            self.assertEqual(evidence["test_verdict"], "PASS")

    def _life(self, root: Path) -> RunLifecycle:
        run = Run.create("f", root / "prompt.md", None, root / "runs" / "r1", root)
        return RunLifecycle(run)

    def test_a_cancelled_dependency_keeps_the_dependent_waiting_not_dispatched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            life = self._life(Path(directory))
            life.run.add_task("A")
            life.run.add_task("B", depends_on=["A"])
            life.run.transition_task("A", "in_progress")
            life.run.transition_task(
                "A", "done", actor=ACTOR_HUMAN, resolution="cancelled",
                note="no longer needed")

            # B's dependency is 'done' but cancelled — not compatible completed-resolution
            # evidence, so B must stay unfinished and undispatched, never terminal.
            self.assertIsNone(_next_actionable(life, ["A", "B"], ["A", "B"]))
            self.assertEqual(life.run.task("B").status, "to_do")
            reason = _pending_reason(life, ["B"])
            self.assertIn("B dependency-not-satisfied: A", reason)

    def test_a_completed_dependency_frees_the_dependent_for_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            life = self._life(Path(directory))
            life.run.add_task("A")
            life.run.add_task("B", depends_on=["A"])
            life.run.transition_task("A", "in_progress")
            self.assertEqual(life.run.record_verdicts("A", "PASS", "PASS"), "done")
            self.assertEqual(life.run.task("A").resolution, "completed")

            self.assertEqual(_next_actionable(life, ["A", "B"], ["A", "B"]), "B")


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
            self.assertEqual(task.task_contract_version, CANONICAL_CONTRACT_VERSION)


if __name__ == "__main__":
    unittest.main()
