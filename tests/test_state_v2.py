"""Schema v2 durable run state: migration, gap-§4 fields, and evidence invariants."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
            self.assertEqual(run.task("LT-1").status, "verified")
            self.assertEqual(run.task("LT-1").attempts, 2)
            self.assertEqual(run.task("LT-2").status, "blocked")
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
            self.assertEqual(stored["artifacts"]["report"], "<repo>\\reports\\EX-1\\executor-1.md")


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
    def _implemented(self, root: Path) -> Run:
        run = _run(root)
        run.add_task("EX-1")
        run.transition_task("EX-1", "ready")
        run.transition_task("EX-1", "running")
        run.transition_task("EX-1", "implemented", ACTOR_EXECUTOR)
        return run

    def test_executor_can_only_produce_implemented_never_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = self._implemented(Path(directory))
            with self.assertRaises(StateError) as caught:
                run.transition_task("EX-1", "verified", ACTOR_EXECUTOR)
            self.assertEqual(caught.exception.code, "unauthorized-transition")

    def test_only_the_runner_may_record_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = self._implemented(Path(directory))
            self.assertEqual(run.transition_task("EX-1", "verified", ACTOR_RUNNER), "verified")
            # verified is terminal: no further transition is defined.
            with self.assertRaises(StateError) as caught:
                run.transition_task("EX-1", "implemented", ACTOR_RUNNER)
            self.assertEqual(caught.exception.code, "illegal-transition")

    def test_authorization_survives_migration_from_v1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "legacy-feature"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(json.dumps(_V1_RUN, indent=2) + "\n",
                                              encoding="utf-8")
            run = Run.load(run_dir, root)
            # LT-1 migrated as 'verified' (terminal) — even the runner cannot move it.
            with self.assertRaises(StateError) as caught:
                run.transition_task("LT-1", "implemented", ACTOR_RUNNER)
            self.assertEqual(caught.exception.code, "illegal-transition")


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


if __name__ == "__main__":
    unittest.main()
