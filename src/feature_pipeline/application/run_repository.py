"""The one commit API: every durable run mutation goes through here.

**DS-03** (``docs/plans/tasks/DS-03_transactional-state-repository.md``, Phase 5 of
``docs/plans/2026-09-03-feature-pipeline-refactor.md``).

``pipeline_core`` mutates run state from many places — ``commands.run_command`` appends to an
in-memory ``Run`` and never flushes, ``diagnostics`` writes a report and separately hopes a
later bulk ``Run.save`` records it, verdict / repair / promotion code path each call
``Run.save`` directly. A crash between any effect and its save loses evidence or leaves state
pointing at a file that is not there (findings F-02 / F-03).

:class:`RunPersistence` is the accepted replacement (``docs/adr/004`` + ``docs/adr/005``).
It composes the transactional
:class:`~feature_pipeline.infrastructure.state.repository.StateRepository` and the
:class:`~feature_pipeline.infrastructure.artifacts.store.FileArtifactStore` over one
:class:`~feature_pipeline.infrastructure.state.run_layout.RunLayout`, and every method follows
the same **publish-before-reference, then compare-and-set commit** sequence:

1. publish the evidence file (``fsync``-ed) — if the process dies here the file is an orphan,
   never a dangling reference;
2. fold the returned :class:`~feature_pipeline.ports.artifacts.ArtifactRef` into a *new*
   immutable state snapshot;
3. commit that snapshot with the caller's ``revision`` token — a stale writer is rejected.

A ``run.json`` therefore never references an unpublished artifact (AC-1), a completed command
survives a forced termination after the commit returns (AC-2), and a resume reconciles any
orphan a crash left behind (AC-3). Callers hold a :class:`LoadedState`, not a mutable ``Run``;
there is no ``save`` to call.

Standard library only.
"""

from __future__ import annotations

from typing import Callable

from feature_pipeline.infrastructure.artifacts.store import FileArtifactStore
from feature_pipeline.infrastructure.state.repository import StateRepository
from feature_pipeline.infrastructure.state.run_layout import (
    RunIdentityPolicy,
    RunLayout,
    allocate_run,
    mint_run_id,
    resume_run,
    write_latest,
)
from feature_pipeline.infrastructure.state.schema_v3 import (
    CommandRecordV3,
    RunStateV3,
    UnknownFieldPolicy,
)
from feature_pipeline.ports.artifacts import ArtifactRef, OrphanReport
from feature_pipeline.ports.clock import Clock
from feature_pipeline.ports.state_repository import LoadedState

#: A test seam. :class:`RunPersistence` calls it with a stage name at each transaction step
#: (``"publish"``, ``"reference"``, ``"commit"``); a fault test raises from it to simulate a
#: crash between steps. ``None`` in production.
StepHook = Callable[[str], None] | None


def _step(hook: StepHook, name: str) -> None:
    if hook is not None:
        hook(name)


class RunPersistence:
    """Transactional persistence for one run directory — the only writer of its state."""

    def __init__(
        self,
        layout: RunLayout,
        *,
        repository: StateRepository | None = None,
        store: FileArtifactStore | None = None,
        fsync: bool = True,
    ) -> None:
        self.layout = layout
        self._repo = repository or StateRepository()
        self._store = store or FileArtifactStore(layout, fsync=fsync)

    # -- reads --------------------------------------------------------------------------

    def open(self, *, unknown_fields: UnknownFieldPolicy = "reject") -> LoadedState:
        """Load the run state paired with its ``revision`` token."""
        return self._repo.load_with_revision(
            self.layout.state_path, unknown_fields=unknown_fields
        )

    def resume(
        self, *, unknown_fields: UnknownFieldPolicy = "reject"
    ) -> tuple[LoadedState, tuple[OrphanReport, ...]]:
        """Load the state and reconcile any orphan evidence a prior crash left behind.

        An evidence file under ``reports/`` / ``logs/`` that the committed state does not
        reference is moved to ``orphans/`` (``docs/adr/004``) — reported, never deleted,
        never adopted.
        """
        loaded = self.open(unknown_fields=unknown_fields)
        orphans = self._store.reconcile_orphans(loaded.state.referenced_artifacts())
        return loaded, orphans

    # -- writes ------------------------------------------------------------------------

    def commit(self, loaded: LoadedState, state: RunStateV3) -> LoadedState:
        """Compare-and-set commit of an already-built snapshot (verdicts, repairs, moves).

        No artifact is involved; the snapshot is written with the held ``revision`` token
        and a stale writer is rejected.
        """
        committed = self._repo.commit(
            state, self.layout.state_path, expected_revision=loaded.revision
        )
        return LoadedState(state=committed, revision=committed.revision)

    def record_command(
        self,
        loaded: LoadedState,
        command: CommandRecordV3,
        *,
        log: str | None = None,
        on_step: StepHook = None,
    ) -> LoadedState:
        """Durably record one completed command and (optionally) its captured log.

        The log is published and ``fsync``-ed first; only then is a snapshot that appends
        ``command`` and references the log committed. After this returns, killing the
        process still leaves the command visible on a fresh load (AC-2).
        """
        snapshot = loaded.state.with_appended_command(command)
        _step(on_step, "reserve")
        if log is not None:
            ref = self._store.publish("log", f"{command.id}.log", log)
            _step(on_step, "publish")
            snapshot = snapshot.with_artifact(f"command:{command.id}", ref.relative_path)
        _step(on_step, "reference")
        committed = self._repo.commit(
            snapshot, self.layout.state_path, expected_revision=loaded.revision
        )
        _step(on_step, "commit")
        return LoadedState(state=committed, revision=committed.revision)

    def record_report(
        self,
        loaded: LoadedState,
        *,
        key: str,
        name: str,
        content: str,
        mutate: Callable[[RunStateV3, ArtifactRef], RunStateV3] | None = None,
        on_step: StepHook = None,
    ) -> tuple[LoadedState, ArtifactRef]:
        """Publish a report (diagnostic, executor report, findings) then commit its ref.

        ``mutate`` folds the published :class:`ArtifactRef` into the snapshot however the
        caller needs (a task's ``executor_report``, a stage entry); by default the ref is
        registered under ``artifacts[key]``. The file is on disk before the commit, so state
        never names a missing report (AC-1).
        """
        _step(on_step, "reserve")
        ref = self._store.publish("report", name, content)
        _step(on_step, "publish")
        snapshot = loaded.state.with_artifact(key, ref.relative_path)
        if mutate is not None:
            snapshot = mutate(snapshot, ref)
        _step(on_step, "reference")
        committed = self._repo.commit(
            snapshot, self.layout.state_path, expected_revision=loaded.revision
        )
        _step(on_step, "commit")
        return LoadedState(state=committed, revision=committed.revision), ref


def initialise_run(
    storage_root: str,
    feature: str,
    initial_state: RunStateV3,
    *,
    clock: Clock,
    policy: RunIdentityPolicy = "unique-run",
    entropy: str | None = None,
) -> tuple[RunLayout, LoadedState]:
    """Allocate a fresh, isolated run directory and commit its first state snapshot.

    A fresh run always lands in its own ``<run-id>/`` (``docs/adr/004``); it can neither
    overwrite an existing run's ``run.json`` nor inherit its evidence. ``latest.json`` is
    repointed last, after the state commit succeeds.
    """
    run_id = mint_run_id(clock, entropy=entropy)
    layout = allocate_run(storage_root, feature, run_id, policy=policy)
    committed = StateRepository().initialise(initial_state, layout.state_path)
    write_latest(layout)
    return layout, LoadedState(state=committed, revision=committed.revision)


def resume_existing_run(
    storage_root: str, feature: str, run_id: str | None = None
) -> RunPersistence:
    """Bind a :class:`RunPersistence` to an existing run (explicit id or ``latest.json``)."""
    return RunPersistence(resume_run(storage_root, feature, run_id))


__all__ = [
    "RunPersistence",
    "StepHook",
    "initialise_run",
    "resume_existing_run",
]
