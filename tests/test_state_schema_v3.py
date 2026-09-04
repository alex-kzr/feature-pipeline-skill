"""Strict schema-v3 durable-state boundary and the deterministic v2 -> v3 migration.

**DS-02** (``docs/plans/tasks/DS-02_state-schema-v3-migration.md``, Phase 5 of
``docs/plans/2026-09-03-feature-pipeline-refactor.md``).

The three acceptance criteria, each pinned by a class below:

* **AC-1** — every preserved schema-v2 fixture migrates to v3 without data loss.
* **AC-2** — corrupt or future state fails *at load* with a field-level diagnostic
  (a stable ``code`` and the offending ``field`` path), and the source file is never
  rewritten.
* **AC-3** — loading never writes; an explicit repository commit is the only writer and
  emits deterministic schema v3, with ``schema_version`` kept distinct from the unrelated
  task-spec ``contract_version``.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from feature_pipeline.infrastructure.state import (
    STATE_SCHEMA_VERSION,
    FieldTypeError,
    FieldValueError,
    MissingFieldError,
    RunStateV3,
    SchemaVersionError,
    StateRepository,
    StateSchemaError,
    UnknownFieldError,
    load_state,
    migrate_v2_to_v3,
)
from schemas.contracts import SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "state"
_V2_FIXTURES = sorted((_FIXTURES / "v2").glob("*.json"))
_MALFORMED = _FIXTURES / "malformed"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class MigrationPreservesEveryRecordedFact(unittest.TestCase):
    """AC-1 — valid v2 fixtures migrate to v3 with no data loss."""

    def test_fixtures_are_present(self) -> None:
        self.assertTrue(_V2_FIXTURES, "no fixtures/state/v2/*.json fixtures found")

    def test_schema_version_is_stamped_to_three(self) -> None:
        for path in _V2_FIXTURES:
            with self.subTest(path.name):
                migrated = migrate_v2_to_v3(_read(path))
                self.assertEqual(migrated["schema_version"], 3)
                self.assertEqual(STATE_SCHEMA_VERSION, 3)

    def test_every_task_fact_survives_migration(self) -> None:
        for path in _V2_FIXTURES:
            with self.subTest(path.name):
                source = _read(path)
                migrated = migrate_v2_to_v3(source)
                self.assertEqual(len(migrated["tasks"]), len(source["tasks"]))
                for before, after in zip(source["tasks"], migrated["tasks"]):
                    for key, value in before.items():
                        self.assertEqual(after[key], value, f"{path.name}:{before['id']}.{key}")

    def test_run_level_facts_survive_migration(self) -> None:
        for path in _V2_FIXTURES:
            with self.subTest(path.name):
                source = _read(path)
                migrated = migrate_v2_to_v3(source)
                for key in (
                    "feature", "prompt_path", "plan_path", "run_id", "status",
                    "current_task", "controls", "environment", "history", "commands",
                    "stages", "artifacts",
                ):
                    self.assertEqual(migrated[key], source[key], f"{path.name}:{key}")

    def test_migration_is_pure_and_deterministic(self) -> None:
        for path in _V2_FIXTURES:
            with self.subTest(path.name):
                source = _read(path)
                snapshot = json.loads(json.dumps(source))
                first = migrate_v2_to_v3(source)
                second = migrate_v2_to_v3(source)
                self.assertEqual(first, second)
                self.assertEqual(source, snapshot, "input mapping was mutated")

    def test_migrated_state_parses_and_round_trips_through_the_dto(self) -> None:
        for path in _V2_FIXTURES:
            with self.subTest(path.name):
                migrated = migrate_v2_to_v3(_read(path))
                state = RunStateV3.from_mapping(migrated)
                self.assertEqual(state.schema_version, 3)
                self.assertEqual(state.to_mapping(), migrated)

    def test_load_state_accepts_a_v2_payload_directly(self) -> None:
        state = load_state(_read(_FIXTURES / "v2" / "run-state-basic.json"))
        self.assertIsInstance(state, RunStateV3)
        self.assertEqual(state.schema_version, 3)
        self.assertEqual(state.task("T-03").blocker, "external dependency unavailable")


class SchemaVersionIsDistinctFromContractVersion(unittest.TestCase):
    """AC-3 (part) — the persistence schema version is not the task-spec contract version."""

    def test_contract_version_is_carried_and_separate(self) -> None:
        state = load_state(_read(_FIXTURES / "v2" / "run-state-basic.json"))
        self.assertEqual(state.schema_version, STATE_SCHEMA_VERSION)
        self.assertEqual(state.contract_version, CONTRACT_SCHEMA_VERSION)
        self.assertNotEqual(state.schema_version, state.contract_version)
        emitted = state.to_mapping()
        self.assertEqual(emitted["schema_version"], 3)
        self.assertEqual(emitted["contract_version"], CONTRACT_SCHEMA_VERSION)


class CorruptOrFutureStateFailsWithFieldLevelDiagnostics(unittest.TestCase):
    """AC-2 — every denial path raises a typed error naming the offending field."""

    def _expect(self, fixture: str, exc: type, code: str, field: str) -> None:
        raw = _read(_MALFORMED / fixture)
        with self.assertRaises(exc) as caught:
            load_state(raw)
        self.assertIsInstance(caught.exception, StateSchemaError)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.field, field)

    def test_unknown_future_schema_version(self) -> None:
        self._expect(
            "unknown-schema-version.json", SchemaVersionError,
            "unsupported-schema-version", "schema_version",
        )

    def test_missing_required_field(self) -> None:
        self._expect(
            "missing-required-field.json", MissingFieldError, "missing-field", "tasks[1].id",
        )

    def test_wrong_field_type(self) -> None:
        self._expect(
            "wrong-field-type.json", FieldTypeError, "invalid-field-type",
            "tasks[0].attempts",
        )

    def test_invalid_enum_value(self) -> None:
        self._expect(
            "invalid-status.json", FieldValueError, "invalid-field-value", "tasks[0].status",
        )

    def test_invalid_nested_evidence_is_not_coerced(self) -> None:
        self._expect(
            "invalid-nested-evidence.json", FieldTypeError, "invalid-field-type",
            "tasks[0].execution_evidence.implementation",
        )

    def test_unknown_field_is_rejected_by_default(self) -> None:
        self._expect(
            "unknown-field.json", UnknownFieldError, "unknown-field",
            "tasks[0].mystery_field",
        )

    def test_a_failed_load_never_rewrites_the_source_file(self) -> None:
        for fixture in sorted(_MALFORMED.glob("*.json")):
            with self.subTest(fixture.name):
                original = fixture.read_bytes()
                with self.assertRaises(StateSchemaError):
                    load_state(_read(fixture))
                self.assertEqual(fixture.read_bytes(), original)


class VersionRoutingFailsClosed(unittest.TestCase):
    """AC-2 — every non-v2/v3 entry point is rejected at the ``schema_version`` field."""

    def test_migrate_rejects_a_non_v2_payload(self) -> None:
        migrated = migrate_v2_to_v3(_read(_FIXTURES / "v2" / "run-state-basic.json"))
        with self.assertRaises(SchemaVersionError) as caught:
            migrate_v2_to_v3(migrated)
        self.assertEqual(caught.exception.field, "schema_version")

    def test_load_state_rejects_missing_schema_version(self) -> None:
        raw = _read(_FIXTURES / "v2" / "run-state-basic.json")
        raw.pop("schema_version")
        with self.assertRaises(SchemaVersionError) as caught:
            load_state(raw)
        self.assertEqual(caught.exception.field, "schema_version")

    def test_load_state_rejects_a_non_integer_schema_version(self) -> None:
        raw = dict(_read(_FIXTURES / "v2" / "run-state-basic.json"), schema_version="2")
        with self.assertRaises(SchemaVersionError):
            load_state(raw)

    def test_load_state_rejects_schema_version_one(self) -> None:
        raw = dict(_read(_FIXTURES / "v2" / "run-state-basic.json"), schema_version=1)
        with self.assertRaises(SchemaVersionError) as caught:
            load_state(raw)
        self.assertEqual(caught.exception.code, "unsupported-schema-version")


class UnknownFieldPolicyIsExplicit(unittest.TestCase):
    """AC-2 (part) — the unknown-field policy is a caller choice, not a silent default."""

    def _payload_with_extra(self) -> dict:
        payload = migrate_v2_to_v3(_read(_FIXTURES / "v2" / "run-state-basic.json"))
        payload["tasks"][0]["mystery_field"] = "surprise"
        payload["undocumented_run_key"] = 7
        return payload

    def test_reject_is_the_default(self) -> None:
        with self.assertRaises(UnknownFieldError):
            RunStateV3.from_mapping(self._payload_with_extra())

    def test_ignore_drops_unknown_keys(self) -> None:
        state = RunStateV3.from_mapping(self._payload_with_extra(), unknown_fields="ignore")
        self.assertNotIn("mystery_field", state.to_mapping()["tasks"][0])
        self.assertNotIn("undocumented_run_key", state.to_mapping())

    def test_preserve_round_trips_unknown_keys(self) -> None:
        state = RunStateV3.from_mapping(self._payload_with_extra(), unknown_fields="preserve")
        emitted = state.to_mapping()
        self.assertEqual(emitted["tasks"][0]["mystery_field"], "surprise")
        self.assertEqual(emitted["undocumented_run_key"], 7)


class LoadingNeverWritesAndCommitIsTheOnlyWriter(unittest.TestCase):
    """AC-3 — load is read-only; commit emits deterministic schema v3."""

    def _basic_state_file(self, tmp: Path) -> Path:
        path = tmp / "run.json"
        path.write_bytes((_FIXTURES / "v2" / "run-state-basic.json").read_bytes())
        return path

    def test_repository_load_does_not_touch_the_file_or_its_directory(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            path = self._basic_state_file(tmp)
            before_bytes = path.read_bytes()
            before_listing = sorted(p.name for p in tmp.iterdir())

            state = StateRepository().load(path)

            self.assertIsInstance(state, RunStateV3)
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(sorted(p.name for p in tmp.iterdir()), before_listing)

    def test_commit_writes_schema_v3_and_bumps_the_revision(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            repo = StateRepository()
            state = repo.load(self._basic_state_file(tmp))
            self.assertEqual(state.revision, 0)

            out = tmp / "committed.json"
            committed = repo.commit(state, out)

            self.assertEqual(committed.revision, 1)
            stored = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(stored["schema_version"], 3)
            self.assertEqual(stored["revision"], 1)
            self.assertEqual(out.read_text(encoding="utf-8")[-1], "\n")

    def test_commit_output_is_deterministic_for_equivalent_state(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            repo = StateRepository()
            state = repo.load(self._basic_state_file(tmp))

            first = tmp / "a.json"
            second = tmp / "b.json"
            repo.commit(state, first)
            repo.commit(state, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_migrated_state_round_trips_through_an_explicit_commit(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            repo = StateRepository()
            migrated = migrate_v2_to_v3(_read(_FIXTURES / "v2" / "run-state-full.json"))
            state = RunStateV3.from_mapping(migrated)

            out = tmp / "run.json"
            repo.commit(state, out)
            reloaded = repo.load(out)

            expected = dict(migrated, revision=1)
            self.assertEqual(reloaded.to_mapping(), expected)


if __name__ == "__main__":
    unittest.main()
