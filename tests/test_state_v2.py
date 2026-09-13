"""Schema v2 durable run state: migration, gap-§4 fields, and evidence invariants."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from pipeline_core.plan import AmendmentError, AmendmentRevision
from pipeline_core.state import (
    ACTOR_EXECUTOR,
    ACTOR_RUNNER,
    Run,
    StateError,
    migrate_run_state,
)

_V1_RUN = {
    "schema_version": 1,
    "feature": "legacy-feature",
    "prompt_path": "prompts/feature.md",
    "plan_path": "plan.json",
    "run_id": "2026-08-30T00-00-00Z-legacy-feature",
    "status": "running",
    "tasks": [
        {"id": "LT-1", "status": "verified", "depends_on": [], "attempts": 2,
         "blocker": None},
        {"id": "LT-2", "status": "blocked", "depends_on": ["LT-1"], "attempts": 1,
         "blocker": "dependency-not-satisfied: LT-1"},
    ],
    "history": [
        {"at": "2026-08-30T00:00:01Z", "scope": "task:LT-1", "from": "pending",
         "to": "ready", "actor": "runner", "note": None},
    ],
    "commands": [
        {"id": "command-1", "stage": "verify", "cwd": ".", "argv": ["python", "-m", "pytest"],
         "exit_code": 0, "duration": 1.5, "stdout": "ok", "stderr": ""},
    ],
}


def _run(root: Path, *, feature: str = "schema-v2") -> Run:
    prompt = root / "prompts" / "feature.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("feature", encoding="utf-8")
    return Run.create(feature, prompt, None, root / "runs" / feature, root)


class SchemaVersionTests(unittest.TestCase):
    def test_new_run_persists_schema_version_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            path = run.save()
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["schema_version"], 2)


class MigrationTests(unittest.TestCase):
    def test_v1_run_loads_and_migrates_preserving_all_recorded_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "legacy-feature"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(json.dumps(_V1_RUN, indent=2) + "\n",
                                              encoding="utf-8")
            run = Run.load(run_dir, root)
            self.assertEqual(run.run_id, _V1_RUN["run_id"])
            self.assertEqual(run.feature, "legacy-feature")
            self.assertEqual(run.status, "running")
            self.assertEqual(list(run.tasks), ["LT-1", "LT-2"])
            self.assertEqual(run.task("LT-1").status, "done")
            # RLC-01 AC-4: a legacy 'verified' status must load as 'done' *with* the
            # completion resolution set — not just the status remapped.
            self.assertEqual(run.task("LT-1").resolution, "completed")
            self.assertEqual(run.task("LT-1").attempts, 2)
            self.assertEqual(run.task("LT-2").status, "in_progress")
            self.assertEqual(run.task("LT-2").blocker, "dependency-not-satisfied: LT-1")
            self.assertEqual(run.task("LT-2").depends_on, ["LT-1"])
            self.assertEqual(run.history, _V1_RUN["history"])
            self.assertEqual(run.commands, _V1_RUN["commands"])
            self.assertEqual(run.to_dict()["schema_version"], 2)

    def test_migrate_run_state_is_pure_and_deterministic(self) -> None:
        source = json.loads(json.dumps(_V1_RUN))
        first = migrate_run_state(source)
        second = migrate_run_state(source)
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(source, _V1_RUN)  # input untouched

    def test_unknown_future_schema_version_fails_closed_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "future"
            run_dir.mkdir(parents=True)
            payload = dict(_V1_RUN, schema_version=99)
            original = json.dumps(payload, indent=2) + "\n"
            (run_dir / "run.json").write_text(original, encoding="utf-8")
            with self.assertRaises(StateError) as caught:
                Run.load(run_dir, root)
            self.assertEqual(caught.exception.code, "unknown-schema-version")
            self.assertEqual((run_dir / "run.json").read_text(encoding="utf-8"), original)

    def test_legacy_verified_load_preserves_source_bytes_through_load_and_continuation(
        self,
    ) -> None:
        """RLC-01 AC-4: loading a legacy v1 'verified' fixture never rewrites the source file,
        and the corrected done/completed resolution survives an unrelated, separately
        committed continuation (not just the first in-memory load)."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "legacy-feature"
            run_dir.mkdir(parents=True)
            run_json = run_dir / "run.json"
            run_json.write_text(json.dumps(_V1_RUN, indent=2) + "\n", encoding="utf-8")
            before_digest = hashlib.sha256(run_json.read_bytes()).hexdigest()

            run = Run.load(run_dir, root)
            after_load_digest = hashlib.sha256(run_json.read_bytes()).hexdigest()
            self.assertEqual(before_digest, after_load_digest)
            self.assertEqual(run.task("LT-1").status, "done")
            self.assertEqual(run.task("LT-1").resolution, "completed")

            # A separately committed continuation unrelated to LT-1 must not lose the fact.
            run.record_event("continuation", to="noted", note="unrelated continuation")
            run.save()
            after_continuation_digest = hashlib.sha256(run_json.read_bytes()).hexdigest()
            self.assertEqual(before_digest, after_continuation_digest)

            reloaded = Run.load(run_dir, root)
            self.assertEqual(reloaded.task("LT-1").status, "done")
            self.assertEqual(reloaded.task("LT-1").resolution, "completed")
            self.assertEqual(reloaded.history[-1]["scope"], "continuation")


class RoundTripTests(unittest.TestCase):
    def test_every_gap_field_on_a_task_survives_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            record = run.add_task("EX-1", depends_on=["EX-0"])
            record.type = "python"
            record.executor = "python-executor"
            record.adapter = "claude"
            record.session_id = "sess-abc"
            record.verification = {"task_verdict": "PASS", "test_verdict": "PASS",
                                   "verified_at": "2026-09-01T10:00:00Z"}
            record.execution_evidence = {
                "attempt": 1, "executor_report": "reports/EX-1/launch-1/executor-1.md",
                "implementation": {"state": "captured",
                                   "changed_files": ["pipeline_core/state.py"],
                                   "manifest": "reports/EX-1/launch-1/implementation-manifest-1.json",
                                   "diff": "reports/EX-1/launch-1/implementation-diff-1.md"},
            }
            record.changed_files = ["pipeline_core/state.py", "tests/test_state_v2.py"]
            record.verification_tier = "scoped"
            record.accepts_scoped = ["EX-0"]
            record.promotion = {"from_run": "prior-run-id", "authorized_by": "human"}
            record.unblocks = ["EX-2"]
            record.maintenance_audit = [{"kind": "maintenance-task-evidence", "ref": "m-1"}]
            record.external_launch_failures = [{"generation": 1, "reason": "adapter-timeout"}]
            record.attested_dependencies = [
                {"dep_id": "EX-0", "source_feature": "elsewhere", "source_run_id": "prior-run-id",
                 "source_digest": "sha256:abc", "task_verdict": "PASS", "test_verdict": "PASS",
                 "verified_at": "2026-09-01T09:00:00Z", "attested_at": "2026-09-01T10:00:00Z"}]
            record.task_path = "docs/plans/tasks/EX-1.md"
            record.task_contract_digest = "sha256:contract"
            record.task_contract_version = "rec09-v1"
            record.reused_verification = [{
                "dependency_id": "EX-0", "source_run_id": "prior-run-id",
                "source_run_digest": "sha256:source", "evidence_identity": "legacy-task-id",
                "task_verdict": "PASS", "test_verdict": "PASS",
                "verified_at": "2026-09-01T09:00:00Z", "reused_at": "2026-09-01T10:00:00Z",
            }]
            record.next_executor_launch_generation = 4
            record.next_task_verifier_launch_generation = 2
            record.next_test_verifier_launch_generation = 3
            run.save()

            reloaded = Run.load(run.run_dir, root)
            self.assertEqual(reloaded.to_dict()["tasks"], run.to_dict()["tasks"])

    def test_run_controls_environment_current_task_and_stages_survive_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.add_task("EX-1")
            run.set_control("adapter", "claude")
            run.set_control("max_repair_attempts", 2, sourced="default")
            run.environment = {"platform": "win32", "python": "3.11.9",
                               "tools": {"claude": "1.2.3"}}
            run.current_task = "EX-1"
            run.record_command("verify", ".", ["python", "-m", "pytest"], 0, 0.1, "ok", "")
            run.artifacts = {"executor_report": "reports/EX-1/launch-1/executor-1.md"}
            run.save()

            reloaded = Run.load(run.run_dir, root)
            self.assertEqual(reloaded.to_dict(), run.to_dict())
            self.assertEqual(reloaded.controls["max_repair_attempts"],
                             {"value": 2, "sourced": "default"})
            self.assertEqual(reloaded.current_task, "EX-1")
            self.assertEqual(reloaded.stage_command_ids("verify"), ["command-1"])

    def test_save_load_is_idempotent_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.add_task("EX-1")
            run.record_command("verify", ".", ["python", "-c", "pass"], 0, 0.0, "", "")
            first = run.save().read_text(encoding="utf-8")
            second = Run.load(run.run_dir, root).save().read_text(encoding="utf-8")
            self.assertEqual(first, second)

    def test_artifact_references_are_redacted_on_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.artifacts = {"report": str(root / "reports" / "EX-1" / "executor-1.md")}
            stored = json.loads(run.save().read_text(encoding="utf-8"))
            self.assertEqual(
                stored["artifacts"]["report"],
                f"<repo>{os.sep}reports{os.sep}EX-1{os.sep}executor-1.md",
            )


class CommandAndGenerationTests(unittest.TestCase):
    def test_command_ids_stay_stable_and_resolvable_after_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.add_task("EX-1")
            run.record_command("verify", ".", ["a"], 0, 0.0, "one", "")
            run.record_command("verify", ".", ["b"], 0, 0.0, "two", "")
            run.save()

            resumed = Run.load(run.run_dir, root)
            resumed.record_command("repair", ".", ["c"], 0, 0.0, "three", "")
            self.assertEqual([c["id"] for c in resumed.commands],
                             ["command-1", "command-2", "command-3"])
            self.assertEqual(resumed.command("command-2")["stdout"], "two")
            self.assertEqual(resumed.stage_command_ids("repair"), ["command-3"])

    def test_launch_generations_are_monotonic_and_per_role_across_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.add_task("EX-1")
            self.assertEqual(run.consume_launch_generation("EX-1", "executor"), 1)
            self.assertEqual(run.consume_launch_generation("EX-1", "executor"), 2)
            self.assertEqual(run.consume_launch_generation("EX-1", "task_verifier"), 1)
            run.save()

            resumed = Run.load(run.run_dir, root)
            self.assertEqual(resumed.consume_launch_generation("EX-1", "executor"), 3)
            self.assertEqual(resumed.consume_launch_generation("EX-1", "task_verifier"), 2)
            self.assertEqual(resumed.consume_launch_generation("EX-1", "test_verifier"), 1)


class ActorAuthorizationTests(unittest.TestCase):
    def _in_progress(self, root: Path) -> Run:
        run = _run(root)
        run.add_task("EX-1")
        run.transition_task("EX-1", "in_progress")
        return run

    def test_executor_cannot_complete_a_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = self._in_progress(Path(directory))
            with self.assertRaises(StateError) as caught:
                run.transition_task("EX-1", "done", ACTOR_EXECUTOR, resolution="completed")
            self.assertEqual(caught.exception.code, "unauthorized-transition")

    def test_only_the_runner_may_record_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = self._in_progress(Path(directory))
            self.assertEqual(run.record_verdicts("EX-1", "PASS", "PASS"), "done")
            # done is terminal: no further transition is defined.
            with self.assertRaises(StateError) as caught:
                run.transition_task("EX-1", "in_progress", ACTOR_RUNNER)
            self.assertEqual(caught.exception.code, "unauthorized-transition")

    def test_authorization_survives_migration_from_v1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "legacy-feature"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(json.dumps(_V1_RUN, indent=2) + "\n",
                                              encoding="utf-8")
            run = Run.load(run_dir, root)
            # LT-1 migrated as 'done' (terminal) — even the runner cannot move it.
            with self.assertRaises(StateError) as caught:
                run.transition_task("LT-1", "in_progress", ACTOR_RUNNER)
            self.assertEqual(caught.exception.code, "unauthorized-transition")


class AttestationTests(unittest.TestCase):
    def test_record_attestation_appends_evidence_and_a_history_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.add_task("EX-1")
            run.add_task("EX-2", depends_on=["EX-1"])
            evidence = {
                "dep_id": "EX-1", "source_feature": "elsewhere", "source_run_id": "src-run",
                "source_digest": "sha256:abc", "task_verdict": "PASS", "test_verdict": "PASS",
                "verified_at": "2026-09-01T09:00:00Z", "attested_at": "2026-09-01T10:00:00Z",
            }
            recorded = run.record_attestation("EX-2", "EX-1", evidence)
            self.assertEqual(recorded, evidence)
            self.assertEqual(run.task("EX-2").attested_dependencies, [evidence])
            self.assertTrue(
                any(e["scope"] == "attestation:EX-2:EX-1" for e in run.history))

    def test_record_attestation_replaces_a_same_dep_id_entry_rather_than_duplicating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.add_task("EX-1")
            run.add_task("EX-2", depends_on=["EX-1"])
            run.record_attestation("EX-2", "EX-1", {"dep_id": "EX-1", "source_feature": "a"})
            run.record_attestation("EX-2", "EX-1", {"dep_id": "EX-1", "source_feature": "b"})
            attested = run.task("EX-2").attested_dependencies
            self.assertEqual(len(attested), 1)
            self.assertEqual(attested[0]["source_feature"], "b")

    def test_task_contract_and_reuse_evidence_are_recorded_only_on_the_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.add_task("EX-1")
            run.set_task_contract("EX-1", "docs/plans/tasks/EX-1.md", "sha256:contract")
            run.record_reused_verification("EX-1", {"dependency_id": "EX-0", "source_run_id": "source"})
            record = run.task("EX-1")
            self.assertEqual(record.task_path, "docs/plans/tasks/EX-1.md")
            self.assertEqual(record.task_contract_digest, "sha256:contract")
            self.assertEqual(record.reused_verification, [{"dependency_id": "EX-0", "source_run_id": "source"}])


class AmendmentRevisionPersistenceTests(unittest.TestCase):
    """TAM-01 AC-2/AC-3: an approved amendment is an immutable, revision-scoped record."""

    def _revision(self, task_id: str = "AM-1", revision: int = 1) -> AmendmentRevision:
        return AmendmentRevision(
            task_id=task_id, revision=revision, prior_digest="sha256:before",
            new_digest="sha256:after", changed_fields=("allowed_scope",),
            added_paths=("tests/test_new_fixture.py",), rationale="baseline exposed scope gap",
            approved_by="a-human", source_evidence="report:launch-2",
            created_at="2026-09-12T00:00:00Z", epoch=1,
        )

    def test_apply_amendment_appends_revision_and_resets_the_repair_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.add_task("AM-1")
            run.transition_task("AM-1", "in_progress")
            run.begin_repair("AM-1", maximum=2)
            run.record_verdicts("AM-1", "FAIL", "FAIL")
            self.assertEqual(run.task("AM-1").attempts, 1)

            revision = self._revision()
            run.apply_amendment(revision, new_digest="sha256:canonical-after",
                                new_digest_version="tam01-amendment-v1")

            record = run.task("AM-1")
            self.assertEqual(record.current_revision, 1)
            self.assertEqual(record.attempts, 0)
            self.assertEqual(record.verification["task_verdict"], None)
            self.assertEqual(record.task_contract_digest, "sha256:canonical-after")
            self.assertEqual(len(record.amendment_revisions), 1)
            self.assertEqual(record.amendment_revisions[0]["approved_by"], "a-human")
            self.assertEqual(record.revision_history, [
                {"revision": 0, "attempts": 1,
                 "verification": {"task_verdict": "FAIL", "test_verdict": "FAIL",
                                  "verified_at": record.revision_history[0]["verification"]["verified_at"]},
                 "contract_digest": None},
            ])

    def test_apply_amendment_rejects_a_task_that_is_already_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.add_task("AM-1")
            run.transition_task("AM-1", "in_progress")
            run.record_verdicts("AM-1", "PASS", "PASS")
            self.assertEqual(run.task("AM-1").status, "done")
            with self.assertRaises(AmendmentError) as ctx:
                run.apply_amendment(self._revision(), new_digest="sha256:x",
                                    new_digest_version="tam01-amendment-v1")
            self.assertEqual(ctx.exception.code, "task-already-done")

    def test_apply_amendment_rejects_an_out_of_order_revision_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.add_task("AM-1")
            with self.assertRaises(AmendmentError) as ctx:
                run.apply_amendment(self._revision(revision=2), new_digest="sha256:x",
                                    new_digest_version="tam01-amendment-v1")
            self.assertEqual(ctx.exception.code, "revision-out-of-order")

    def test_amendment_revisions_and_new_fields_round_trip_through_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _run(root)
            run.add_task("AM-1")
            run.apply_amendment(self._revision(), new_digest="sha256:canonical-after",
                                new_digest_version="tam01-amendment-v1")
            run.save()
            reloaded = Run.load(run.run_dir, root)
            record = reloaded.task("AM-1")
            self.assertEqual(record.current_revision, 1)
            self.assertEqual(len(record.amendment_revisions), 1)
            self.assertEqual(record.amendment_revisions[0]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
