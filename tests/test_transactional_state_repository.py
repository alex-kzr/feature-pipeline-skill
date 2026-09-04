"""Transactional persistence, run-directory identity, and orphan reconciliation.

**DS-03** (``docs/plans/tasks/DS-03_transactional-state-repository.md``, Phase 5 of
``docs/plans/2026-09-03-feature-pipeline-refactor.md``).

The three acceptance criteria, each pinned by a class below:

* **AC-1** — state never references an unpublished artifact, and a stale writer is rejected
  (``StateRevisionConflict``) instead of last-write-wins.
* **AC-2** — a completed command survives a forced termination that happens after the commit
  API returns (``docs/adr/005``).
* **AC-3** — a fresh run lands in its own ``<run-id>/`` and can neither overwrite another
  run's ``run.json`` nor inherit its evidence; a resume reconciles orphan artifacts into
  ``orphans/`` (``docs/adr/004``).

Plus the ADR-005 fault-injection matrix: a crash between any two transaction steps leaves a
state that resume reconciles to "effect not recorded" or "effect fully recorded" — never
"state references missing evidence".
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from feature_pipeline.application.run_repository import (
    RunPersistence,
    initialise_run,
    resume_existing_run,
)
from feature_pipeline.contracts import SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION
from feature_pipeline.infrastructure.artifacts.store import FileArtifactStore
from feature_pipeline.infrastructure.state import durability
from feature_pipeline.infrastructure.state.repository import StateRepository
from feature_pipeline.infrastructure.state.run_layout import (
    RUN_ID_RE,
    ActiveRunPresent,
    RunAlreadyExists,
    RunLayout,
    StaleRunPointer,
    allocate_run,
    mint_run_id,
    read_latest,
    resume_run,
    write_latest,
)
from feature_pipeline.infrastructure.state.schema_v3 import CommandRecordV3, RunStateV3
from feature_pipeline.ports.artifacts import ArtifactNameError
from feature_pipeline.ports.clock import ManualClock
from feature_pipeline.ports.state_repository import LoadedState, StateRevisionConflict

_CLOCK = ManualClock(datetime(2026, 9, 4, 12, 30, 15, tzinfo=timezone.utc))


def _state(**overrides: object) -> RunStateV3:
    mapping: dict[str, object] = {
        "schema_version": 3,
        "contract_version": CONTRACT_SCHEMA_VERSION,
        "revision": 0,
        "feature": "demo",
        "prompt_path": ".prompts/demo.md",
        "plan_path": "docs/plans/demo.md",
        "run_id": "20260904T123015Z-deadbeef",
        "status": "running",
        "tasks": [
            {
                "id": "T-1",
                "status": "running",
                "execution_evidence": {
                    "attempt": 1,
                    "executor_report": None,
                    "implementation": {"state": "not-attempted"},
                },
            }
        ],
    }
    mapping.update(overrides)
    return RunStateV3.from_mapping(mapping)


def _command(command_id: str = "command-1") -> CommandRecordV3:
    return CommandRecordV3(
        id=command_id,
        stage="verification",
        cwd=".",
        argv=("python", "-m", "pytest"),
        exit_code=0,
        duration=1.5,
        stdout="ok",
        stderr="",
    )


def _tmpdir() -> Path:
    path = Path(tempfile.mkdtemp())
    return path


# ======================================================================================
# The ADR-005 crash-safe write primitive.
# ======================================================================================


class TestCrashSafeWrite(unittest.TestCase):
    def test_temp_names_are_unique_per_writer(self) -> None:
        target = _tmpdir() / "run.json"
        names = {durability.unique_temp_name(target).name for _ in range(50)}
        self.assertEqual(len(names), 50)
        for name in names:
            self.assertTrue(name.startswith("run.json."))
            self.assertTrue(name.endswith(".tmp"))

    def test_replace_is_the_publish_step_and_fsync_runs(self) -> None:
        target = _tmpdir() / "nested" / "run.json"
        fsync_calls: list[int] = []
        real_fsync = os.fsync
        try:
            os.fsync = lambda fd: fsync_calls.append(fd) or real_fsync(fd)  # type: ignore[assignment]
            durability.atomic_write(target, b'{"k": 1}')
        finally:
            os.fsync = real_fsync  # type: ignore[assignment]
        self.assertEqual(target.read_bytes(), b'{"k": 1}')
        self.assertGreaterEqual(len(fsync_calls), 1)  # at least the file handle
        # no temp litter left behind
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_failed_write_leaves_no_partial_file(self) -> None:
        target = _tmpdir() / "run.json"

        class Boom(Exception):
            pass

        real_replace = os.replace

        def explode(src: object, dst: object) -> None:
            raise Boom

        try:
            os.replace = explode  # type: ignore[assignment]
            with self.assertRaises(Boom):
                durability.atomic_write(target, b"data")
        finally:
            os.replace = real_replace  # type: ignore[assignment]
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_writer_does_not_redact(self) -> None:
        # F-18: canonical bytes are stored verbatim, secrets and abs paths included.
        target = _tmpdir() / "run.json"
        payload = b'{"token": "AKIA-not-really", "home": "/home/someone/secret"}'
        durability.atomic_write(target, payload)
        self.assertEqual(target.read_bytes(), payload)


# ======================================================================================
# AC-1 — no dangling references; stale writers rejected.
# ======================================================================================


class TestRevisionCompareAndSet(unittest.TestCase):
    def test_second_writer_of_the_same_revision_is_rejected(self) -> None:
        path = _tmpdir() / "run.json"
        repo = StateRepository()
        repo.initialise(_state(), path)

        first = repo.load_with_revision(path)
        second = repo.load_with_revision(path)
        self.assertEqual(first.revision, second.revision)

        repo.commit(first.state, path, expected_revision=first.revision)

        with self.assertRaises(StateRevisionConflict) as caught:
            repo.commit(second.state, path, expected_revision=second.revision)
        self.assertEqual(caught.exception.code, "state-revision-conflict")
        # the rejected write changed nothing
        self.assertEqual(json.loads(path.read_text())["revision"], first.revision + 1)

    def test_commit_without_expected_revision_keeps_ds02_behaviour(self) -> None:
        path = _tmpdir() / "run.json"
        repo = StateRepository()
        state = repo.initialise(_state(), path)
        committed = repo.commit(state, path)
        self.assertEqual(committed.revision, 1)
        self.assertEqual(json.loads(path.read_text())["revision"], 1)

    def test_initialise_refuses_to_overwrite_an_existing_run_json(self) -> None:
        path = _tmpdir() / "run.json"
        repo = StateRepository()
        repo.initialise(_state(), path)
        with self.assertRaises(StateRevisionConflict):
            repo.initialise(_state(), path)


class TestPublishBeforeReference(unittest.TestCase):
    def _fresh_run(self) -> tuple[RunPersistence, LoadedState]:
        storage = _tmpdir()
        layout, loaded = initialise_run(
            str(storage), "demo", _state(), clock=_CLOCK, entropy="deadbeef"
        )
        return RunPersistence(layout), loaded

    def test_crash_before_publish_records_nothing(self) -> None:
        persistence, loaded = self._fresh_run()

        def crash(step: str) -> None:
            if step == "reserve":
                raise RuntimeError("killed")

        with self.assertRaises(RuntimeError):
            persistence.record_command(loaded, _command(), log="log body", on_step=crash)

        after = persistence.open()
        self.assertEqual(after.state.commands, ())
        self.assertEqual(list(persistence.layout.logs_dir.iterdir()), [])

    def test_crash_after_publish_leaves_an_orphan_not_a_dangling_ref(self) -> None:
        persistence, loaded = self._fresh_run()

        def crash(step: str) -> None:
            if step == "reference":
                raise RuntimeError("killed after fsync, before commit")

        with self.assertRaises(RuntimeError):
            persistence.record_command(loaded, _command(), log="log body", on_step=crash)

        # the log file is on disk ...
        orphan = persistence.layout.logs_dir / "command-1.log"
        self.assertTrue(orphan.is_file())
        # ... but the committed state does not mention it
        after = persistence.open()
        self.assertEqual(after.state.commands, ())
        self.assertNotIn(
            "logs/command-1.log", after.state.referenced_artifacts()
        )
        # resume sweeps it into orphans/
        _, orphans = persistence.resume()
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0].original, "logs/command-1.log")
        self.assertFalse(orphan.exists())
        self.assertTrue(
            (persistence.layout.orphans_dir / "logs" / "command-1.log").is_file()
        )


# ======================================================================================
# AC-2 — completed commands survive forced termination.
# ======================================================================================


class TestCompletedCommandDurable(unittest.TestCase):
    def test_command_and_log_are_on_disk_once_record_command_returns(self) -> None:
        storage = _tmpdir()
        layout, loaded = initialise_run(
            str(storage), "demo", _state(), clock=_CLOCK, entropy="deadbeef"
        )
        persistence = RunPersistence(layout)

        updated = persistence.record_command(loaded, _command(), log="captured output")

        # simulate a hard kill: drop every in-memory handle, reload from disk alone.
        del persistence, loaded, updated
        reloaded = StateRepository().load(layout.state_path)
        self.assertEqual(len(reloaded.commands), 1)
        self.assertEqual(reloaded.commands[0].id, "command-1")
        self.assertIn("logs/command-1.log", reloaded.referenced_artifacts())
        self.assertEqual(
            (layout.logs_dir / "command-1.log").read_text(encoding="utf-8"),
            "captured output",
        )

    def test_revision_advances_once_per_recorded_command(self) -> None:
        storage = _tmpdir()
        layout, loaded = initialise_run(
            str(storage), "demo", _state(), clock=_CLOCK, entropy="deadbeef"
        )
        persistence = RunPersistence(layout)
        self.assertEqual(loaded.revision, 0)
        loaded = persistence.record_command(loaded, _command("command-1"), log="a")
        self.assertEqual(loaded.revision, 1)
        loaded = persistence.record_command(loaded, _command("command-2"), log="b")
        self.assertEqual(loaded.revision, 2)
        self.assertEqual(len(persistence.open().state.commands), 2)


# ======================================================================================
# AC-3 — fresh-run isolation and orphan reconciliation on resume.
# ======================================================================================


class TestFreshRunIsolation(unittest.TestCase):
    def test_same_second_run_ids_are_distinct(self) -> None:
        a = mint_run_id(_CLOCK)
        b = mint_run_id(_CLOCK)
        self.assertNotEqual(a, b)
        self.assertTrue(RUN_ID_RE.match(a))
        self.assertTrue(a.startswith("20260904T123015Z-"))

    def test_second_fresh_run_does_not_touch_the_first(self) -> None:
        storage = _tmpdir()
        layout_a, loaded_a = initialise_run(
            str(storage), "demo", _state(), clock=_CLOCK, entropy="aaaaaaaa"
        )
        persistence_a = RunPersistence(layout_a)
        persistence_a.record_command(loaded_a, _command(), log="run A output")
        digest_a = layout_a.state_path.read_bytes()

        layout_b, _ = initialise_run(
            str(storage), "demo", _state(), clock=_CLOCK, entropy="bbbbbbbb"
        )

        self.assertNotEqual(layout_a.run_dir, layout_b.run_dir)
        # run A's state and evidence are untouched
        self.assertEqual(layout_a.state_path.read_bytes(), digest_a)
        self.assertTrue((layout_a.logs_dir / "command-1.log").is_file())
        # latest.json now names run B
        self.assertEqual(read_latest(str(storage), "demo"), layout_b.run_id)

    def test_allocate_refuses_an_existing_run_directory(self) -> None:
        storage = _tmpdir()
        run_id = mint_run_id(_CLOCK, entropy="deadbeef")
        allocate_run(str(storage), "demo", run_id)
        with self.assertRaises(RunAlreadyExists):
            allocate_run(str(storage), "demo", run_id)

    def test_single_active_run_policy_refuses_a_second_fresh_start(self) -> None:
        storage = _tmpdir()
        initialise_run(
            str(storage), "demo", _state(), clock=_CLOCK, entropy="aaaaaaaa",
            policy="single-active-run",
        )
        with self.assertRaises(ActiveRunPresent):
            initialise_run(
                str(storage), "demo", _state(), clock=_CLOCK, entropy="bbbbbbbb",
                policy="single-active-run",
            )


class TestResumeReconcilesOrphans(unittest.TestCase):
    def test_resume_quarantines_an_unreferenced_evidence_file(self) -> None:
        storage = _tmpdir()
        layout, loaded = initialise_run(
            str(storage), "demo", _state(), clock=_CLOCK, entropy="deadbeef"
        )
        persistence = RunPersistence(layout)
        loaded = persistence.record_command(loaded, _command(), log="referenced")

        # a crash left this behind, unreferenced
        (layout.logs_dir / "command-99.log").write_text("orphan", encoding="utf-8")

        resumed, orphans = persistence.resume()
        self.assertEqual([o.original for o in orphans], ["logs/command-99.log"])
        self.assertFalse((layout.logs_dir / "command-99.log").exists())
        self.assertTrue((layout.orphans_dir / "logs" / "command-99.log").is_file())
        # the referenced log is left alone
        self.assertTrue((layout.logs_dir / "command-1.log").is_file())
        self.assertEqual(resumed.state.commands[0].id, "command-1")

    def test_resume_by_run_id_and_by_latest_pointer(self) -> None:
        storage = _tmpdir()
        layout, _ = initialise_run(
            str(storage), "demo", _state(), clock=_CLOCK, entropy="deadbeef"
        )
        by_latest = resume_run(str(storage), "demo")
        by_id = resume_run(str(storage), "demo", layout.run_id)
        self.assertEqual(by_latest.run_dir, layout.run_dir)
        self.assertEqual(by_id.run_dir, layout.run_dir)

    def test_stale_latest_pointer_fails_closed(self) -> None:
        storage = _tmpdir()
        layout, _ = initialise_run(
            str(storage), "demo", _state(), clock=_CLOCK, entropy="deadbeef"
        )
        # point latest.json at a run directory that is not there
        (storage / "demo" / "latest.json").write_text(
            json.dumps({"run_id": "20260101T000000Z-00000000"}), encoding="utf-8"
        )
        with self.assertRaises(StaleRunPointer):
            read_latest(str(storage), "demo")
        with self.assertRaises(StaleRunPointer):
            resume_existing_run(str(storage), "demo")


# ======================================================================================
# ADR-005 fault-injection matrix.
# ======================================================================================


class TestFaultInjectionMatrix(unittest.TestCase):
    STEPS = ("reserve", "publish", "reference", "commit")

    def test_a_crash_between_any_two_steps_never_leaves_a_dangling_reference(self) -> None:
        for crash_step in self.STEPS:
            with self.subTest(crash_step=crash_step):
                storage = _tmpdir()
                layout, loaded = initialise_run(
                    str(storage), "demo", _state(), clock=_CLOCK, entropy="deadbeef"
                )
                persistence = RunPersistence(layout)

                def crash(step: str, target: str = crash_step) -> None:
                    if step == target:
                        raise RuntimeError(f"killed at {target}")

                raised = False
                try:
                    loaded = persistence.record_command(
                        loaded, _command(), log="body", on_step=crash
                    )
                except RuntimeError:
                    raised = True

                resumed, orphans = persistence.resume()
                referenced = resumed.state.referenced_artifacts()
                # invariant: every referenced evidence path exists on disk
                for rel in referenced:
                    self.assertTrue(
                        (layout.run_dir / rel).is_file(),
                        f"{crash_step}: state references missing {rel}",
                    )
                if raised and crash_step != "commit":
                    # effect not recorded; any published file was swept to orphans/
                    self.assertEqual(resumed.state.commands, ())
                else:
                    # crash at/after commit: the command is fully recorded
                    self.assertEqual(len(resumed.state.commands), 1)
                    self.assertEqual(orphans, ())


class TestOneCommitApi(unittest.TestCase):
    def _fresh(self) -> tuple[RunPersistence, LoadedState, RunLayout]:
        storage = _tmpdir()
        layout, loaded = initialise_run(
            str(storage), "demo", _state(), clock=_CLOCK, entropy="deadbeef"
        )
        return RunPersistence(layout), loaded, layout

    def test_plain_commit_is_compare_and_set(self) -> None:
        persistence, loaded, _ = self._fresh()
        moved = loaded.state.with_task(
            replace(loaded.state.task("T-1"), status="implemented")
        )
        updated = persistence.commit(loaded, moved)
        self.assertEqual(updated.revision, 1)
        self.assertEqual(persistence.open().state.task("T-1").status, "implemented")
        # the stale handle can no longer commit
        with self.assertRaises(StateRevisionConflict):
            persistence.commit(loaded, moved)

    def test_record_report_publishes_then_references(self) -> None:
        persistence, loaded, layout = self._fresh()

        def attach(state: RunStateV3, ref: object) -> RunStateV3:
            entry = state.task("T-1")
            evidence = replace(
                entry.execution_evidence,
                executor_report=ref.relative_path,  # type: ignore[attr-defined]
            )
            return state.with_task(replace(entry, execution_evidence=evidence))

        updated, ref = persistence.record_report(
            loaded,
            key="diagnostic:T-1",
            name="T-1-diagnostic-1.md",
            content="# findings\nall clear\n",
            mutate=attach,
        )
        self.assertEqual(ref.relative_path, "reports/T-1-diagnostic-1.md")
        self.assertTrue((layout.reports_dir / "T-1-diagnostic-1.md").is_file())
        reloaded = StateRepository().load(layout.state_path)
        self.assertEqual(
            reloaded.task("T-1").execution_evidence.executor_report,
            "reports/T-1-diagnostic-1.md",
        )
        self.assertIn("reports/T-1-diagnostic-1.md", reloaded.referenced_artifacts())
        # a subsequent resume finds no orphan — everything is referenced
        _, orphans = persistence.resume()
        self.assertEqual(orphans, ())


class TestArtifactNameValidation(unittest.TestCase):
    def test_names_that_escape_the_run_directory_are_rejected(self) -> None:
        storage = _tmpdir()
        layout, _ = initialise_run(
            str(storage), "demo", _state(), clock=_CLOCK, entropy="deadbeef"
        )
        store = FileArtifactStore(layout)
        for bad in ("../escape", "sub/dir.log", "/abs.log", "", ".", ".."):
            with self.subTest(bad=bad), self.assertRaises(ArtifactNameError):
                store.publish("log", bad, "x")


if __name__ == "__main__":
    unittest.main()
