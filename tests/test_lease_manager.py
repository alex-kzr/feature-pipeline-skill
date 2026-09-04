"""RS-02 - one bounded owner-token lease behind the pipeline, task, and program locks.

Covers the new :mod:`feature_pipeline.ports.locks` port and its
:class:`feature_pipeline.infrastructure.locks.manager.FileLeaseManager`, the per-process
owner identity that survives PID reuse, and the two migrated ``pipeline_core`` call sites
(``pipeline_core.lease.PipelineLease`` and ``pipeline_core.concurrency.FileMutex``).

Multiprocess where the requirement is cross-process: a "live owner" is modelled with this
process's own PID (certainly alive) plus a foreign nonce, and a "dead owner" with a PID that
has provably exited.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from feature_pipeline.infrastructure.locks.manager import FileLeaseManager
from feature_pipeline.infrastructure.locks.owner import current_owner, pid_alive
from feature_pipeline.infrastructure.locks.record import read_holder
from feature_pipeline.ports.locks import (
    DEFAULT_ACQUIRE_TIMEOUT_S,
    Lease,
    LeaseHeld,
    LeaseManager,
    LeaseRequest,
    LeaseTimeout,
    NotLeaseOwner,
    OwnerToken,
    StalenessPolicy,
)

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _dead_pid() -> int:
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait(timeout=30)
    return done.pid


def _write_record(
    path: Path,
    *,
    pid: int,
    run_id: str | None = "holder",
    nonce: str = "f0f0f0f0",
    task_id: str | None = None,
    heartbeat_age_s: float = 0.0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=heartbeat_age_s)).strftime(_TS)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pid": pid,
                "nonce": nonce,
                "task_id": task_id,
                "started_at": stamp,
                "acquired_at": stamp,
                "heartbeat_at": stamp,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


class OwnerIdentityTests(unittest.TestCase):
    def test_current_owner_nonce_is_stable_within_the_process(self) -> None:
        self.assertEqual(current_owner("r").nonce, current_owner("r").nonce)
        self.assertEqual(current_owner("r").pid, os.getpid())

    def test_matches_requires_run_pid_and_nonce(self) -> None:
        token = OwnerToken(run_id="run-a", pid=4321, nonce="abcd")
        self.assertTrue(token.matches({"run_id": "run-a", "pid": 4321, "nonce": "abcd"}))
        self.assertFalse(token.matches({"run_id": "run-a", "pid": 4321, "nonce": "ffff"}))
        self.assertFalse(token.matches({"run_id": "run-b", "pid": 4321, "nonce": "abcd"}))
        self.assertFalse(token.matches({"run_id": "run-a", "pid": 9999, "nonce": "abcd"}))

    def test_a_legacy_record_without_a_nonce_matches_on_run_and_pid(self) -> None:
        token = OwnerToken(run_id="run-a", pid=4321, nonce="abcd")
        self.assertTrue(token.matches({"run_id": "run-a", "pid": 4321, "task_id": None}))

    def test_a_none_run_id_token_ignores_the_recorded_run_id(self) -> None:
        token = OwnerToken(run_id=None, pid=4321, nonce="abcd")
        self.assertTrue(token.matches({"run_id": "anything", "pid": 4321, "nonce": "abcd"}))
        self.assertFalse(token.matches({"run_id": "anything", "pid": 1, "nonce": "abcd"}))

    def test_pid_alive_probe_matches_reality(self) -> None:
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(_dead_pid()))
        self.assertFalse(pid_alive(-1))


class PortContractTests(unittest.TestCase):
    def test_manager_and_lease_satisfy_the_protocols(self) -> None:
        with TemporaryDirectory() as directory:
            manager = FileLeaseManager()
            self.assertIsInstance(manager, LeaseManager)
            lease = manager.lease(
                LeaseRequest(path=str(Path(directory) / "x.lock"), owner=current_owner("r"))
            )
            self.assertIsInstance(lease, Lease)

    def test_default_acquire_timeout_is_bounded_not_infinite(self) -> None:
        # AC-1 - no supported acquisition waits forever by default.
        self.assertIsInstance(DEFAULT_ACQUIRE_TIMEOUT_S, float)
        self.assertGreater(DEFAULT_ACQUIRE_TIMEOUT_S, 0.0)
        self.assertEqual(LeaseRequest(path="p", owner=current_owner("r")).acquire_timeout_s,
                         DEFAULT_ACQUIRE_TIMEOUT_S)


class AcquireReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.path = self.root / "pipeline.lock"
        self.manager = FileLeaseManager()
        self.addCleanup(self._dir.cleanup)

    def _lease(self, owner: OwnerToken, **kw: object) -> Lease:
        return self.manager.lease(LeaseRequest(path=str(self.path), owner=owner, **kw))  # type: ignore[arg-type]

    def test_round_trip_creates_then_removes_the_lock_file(self) -> None:
        lease = self._lease(current_owner("run-a"), task_id="T-1")
        lease.acquire()
        self.assertTrue(self.path.exists())
        self.assertTrue(lease.held)
        lease.release()
        self.assertFalse(self.path.exists())
        self.assertFalse(lease.held)

    def test_lock_file_keeps_the_visible_compatibility_fields(self) -> None:
        self._lease(current_owner("run-a"), task_id="T-9").acquire()
        record = json.loads(self.path.read_text(encoding="utf-8"))
        for key in ("run_id", "pid", "task_id", "started_at", "nonce", "heartbeat_at"):
            self.assertIn(key, record)
        self.assertEqual(record["run_id"], "run-a")
        self.assertEqual(record["task_id"], "T-9")

    def test_the_record_is_still_parseable_by_pipeline_core_read_lease(self) -> None:
        from pipeline_core.state import read_lease

        self._lease(current_owner("run-a"), task_id="T-1").acquire()
        held = read_lease(self.path)
        assert held is not None
        self.assertEqual(held["run_id"], "run-a")
        self.assertEqual(held["pid"], os.getpid())

    def test_a_live_owner_with_a_zero_timeout_fails_closed(self) -> None:
        _write_record(self.path, pid=os.getpid(), nonce="foreign")
        with self.assertRaises(LeaseHeld) as ctx:
            self._lease(current_owner("run-a"), acquire_timeout_s=0).acquire()
        self.assertEqual(ctx.exception.code, "lease-held")

    def test_a_live_owner_with_a_finite_timeout_times_out_rather_than_hangs(self) -> None:
        _write_record(self.path, pid=os.getpid(), nonce="foreign")
        start = time.monotonic()
        with self.assertRaises(LeaseTimeout) as ctx:
            self._lease(current_owner("run-a"), acquire_timeout_s=0.4, poll_interval_s=0.02).acquire()
        elapsed = time.monotonic() - start
        self.assertEqual(ctx.exception.code, "lease-timeout")
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 10.0)

    def test_a_provably_dead_owner_is_reclaimed(self) -> None:
        _write_record(self.path, pid=_dead_pid(), nonce="foreign", run_id="old-run")
        lease = self._lease(current_owner("new-run"), acquire_timeout_s=0)
        lease.acquire()
        self.assertEqual(read_holder(self.path).run_id, "new-run")  # type: ignore[union-attr]
        lease.release()

    def test_an_unreadable_lock_is_never_reclaimed(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(LeaseHeld):
            self._lease(current_owner("run-a"), acquire_timeout_s=0.3).acquire()

    def test_a_record_without_a_pid_fails_closed(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"run_id": "x", "pid": None}) + "\n", encoding="utf-8")
        with self.assertRaises(LeaseHeld):
            self._lease(current_owner("run-a"), acquire_timeout_s=0.3).acquire()


class OwnershipTests(unittest.TestCase):
    """AC-2 - only the current owner token can release a lease."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.path = Path(self._dir.name) / "pipeline.lock"
        self.manager = FileLeaseManager()
        self.addCleanup(self._dir.cleanup)

    def _lease(self, owner: OwnerToken, **kw: object) -> Lease:
        return self.manager.lease(LeaseRequest(path=str(self.path), owner=owner, **kw))  # type: ignore[arg-type]

    def test_a_reused_pid_with_a_foreign_nonce_cannot_release(self) -> None:
        owner = current_owner("run-a")
        self._lease(owner).acquire()
        impostor = OwnerToken(run_id="run-a", pid=owner.pid, nonce="not-the-same-process")
        self._lease(impostor).release()
        self.assertTrue(self.path.exists())
        self._lease(owner).release()
        self.assertFalse(self.path.exists())

    def test_release_by_a_non_owner_is_a_silent_no_op(self) -> None:
        _write_record(self.path, pid=os.getpid(), run_id="somebody-else", nonce="foreign")
        self._lease(current_owner("run-a")).release()
        self.assertTrue(self.path.exists())

    def test_touch_by_a_non_owner_raises_not_lease_owner(self) -> None:
        _write_record(self.path, pid=os.getpid(), run_id="somebody-else", nonce="foreign")
        with self.assertRaises(NotLeaseOwner):
            self._lease(current_owner("run-a")).touch()

    def test_touch_by_the_owner_advances_the_heartbeat(self) -> None:
        lease = self._lease(current_owner("run-a"))
        lease.acquire()
        before = read_holder(self.path).heartbeat_at  # type: ignore[union-attr]
        time.sleep(1.1)
        lease.touch()
        after = read_holder(self.path).heartbeat_at  # type: ignore[union-attr]
        assert before is not None and after is not None
        self.assertGreater(after, before)
        lease.release()


class ContextManagerCleanupTests(unittest.TestCase):
    """AC-3 - every migrated path releases across success and failure."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.path = Path(self._dir.name) / "pipeline.lock"
        self.manager = FileLeaseManager()
        self.addCleanup(self._dir.cleanup)

    def _req(self, **kw: object) -> LeaseRequest:
        return LeaseRequest(path=str(self.path), owner=current_owner("run-a"), **kw)  # type: ignore[arg-type]

    def test_success_path_releases(self) -> None:
        with self.manager.lease(self._req()):
            self.assertTrue(self.path.exists())
        self.assertFalse(self.path.exists())

    def test_exception_path_releases_and_propagates(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.manager.lease(self._req()):
                raise RuntimeError("boom")
        self.assertFalse(self.path.exists())

    def test_cancellation_path_releases(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            with self.manager.lease(self._req()):
                raise KeyboardInterrupt
        self.assertFalse(self.path.exists())

    def test_timeout_inside_a_with_block_leaks_nothing_of_ours(self) -> None:
        _write_record(self.path, pid=os.getpid(), nonce="foreign", run_id="live")
        with self.assertRaises(LeaseTimeout):
            with self.manager.lease(self._req(acquire_timeout_s=0.3)):
                self.fail("body must not run when acquisition times out")
        # the foreign live lock is untouched; we created nothing.
        self.assertEqual(read_holder(self.path).run_id, "live")  # type: ignore[union-attr]

    def test_stale_dead_owner_is_reclaimed_through_the_context_manager(self) -> None:
        _write_record(self.path, pid=_dead_pid(), nonce="foreign", run_id="crashed")
        with self.manager.lease(self._req(acquire_timeout_s=0)):
            self.assertEqual(read_holder(self.path).run_id, "run-a")  # type: ignore[union-attr]
        self.assertFalse(self.path.exists())

    def test_pid_reuse_is_held_by_default_but_reclaimable_under_an_explicit_policy(self) -> None:
        _write_record(self.path, pid=os.getpid(), nonce="foreign", heartbeat_age_s=6000)
        with self.assertRaises(LeaseHeld):
            self.manager.lease(self._req(acquire_timeout_s=0)).acquire()

        reclaiming = self._req(
            acquire_timeout_s=0,
            staleness=StalenessPolicy(heartbeat_ttl_s=300.0, require_heartbeat=True),
        )
        lease = self.manager.lease(reclaiming)
        lease.acquire()
        self.assertEqual(read_holder(self.path).run_id, "run-a")  # type: ignore[union-attr]
        lease.release()

    def test_a_fresh_heartbeat_still_blocks_under_the_require_heartbeat_policy(self) -> None:
        _write_record(self.path, pid=os.getpid(), nonce="foreign", heartbeat_age_s=1)
        req = self._req(
            acquire_timeout_s=0,
            staleness=StalenessPolicy(heartbeat_ttl_s=300.0, require_heartbeat=True),
        )
        with self.assertRaises(LeaseHeld):
            self.manager.lease(req).acquire()


class PipelineCoreCutoverTests(unittest.TestCase):
    """The two production call sites now delegate to the one primitive."""

    def test_pipeline_lease_module_delegates_to_the_lease_manager(self) -> None:
        import pipeline_core.lease as lease_mod

        src = inspect.getsource(lease_mod)
        self.assertIn("FileLeaseManager", src)
        acquire_src = inspect.getsource(lease_mod.PipelineLease.acquire)
        self.assertNotIn("os.open", acquire_src)
        self.assertNotIn("O_EXCL", acquire_src)

    def test_concurrency_file_mutex_delegates_to_the_lease_manager(self) -> None:
        import pipeline_core.concurrency as concurrency_mod

        src = inspect.getsource(concurrency_mod)
        self.assertIn("FileLeaseManager", src)
        acquire_src = inspect.getsource(concurrency_mod.FileMutex.acquire)
        self.assertNotIn("os.open", acquire_src)

    def test_file_mutex_default_timeout_is_now_bounded(self) -> None:
        # AC-1 - FileMutex previously defaulted to an unbounded wait.
        from pipeline_core.concurrency import FileMutex, write_mutex

        mutex_default = inspect.signature(FileMutex).parameters["timeout"].default
        wm_default = inspect.signature(write_mutex).parameters["timeout"].default
        self.assertIsInstance(mutex_default, float)
        self.assertGreater(mutex_default, 0.0)
        self.assertIsInstance(wm_default, float)
        self.assertGreater(wm_default, 0.0)

    def test_pipeline_lease_still_raises_lease_held_error_on_a_live_foreign_owner(self) -> None:
        from pipeline_core.lease import LeaseHeldError, PipelineLease
        from pipeline_core.state import write_lease

        with TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.lock"
            write_lease(path, "other-run", os.getpid(), "T-1")
            with self.assertRaises(LeaseHeldError) as ctx:
                PipelineLease(path, "mine").acquire("T-2")
            self.assertEqual(ctx.exception.code, "lease-held")

    def test_pipeline_lease_reclaims_a_dead_owner_and_denies_an_unreadable_one(self) -> None:
        from pipeline_core.concurrency import PipelineLease
        from pipeline_core.lease import LeaseHeldError
        from pipeline_core.state import read_lease, write_lease

        with TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.lock"
            write_lease(path, "old-run", _dead_pid(), "T-1")
            PipelineLease(path, "new-run").acquire("T-1")
            self.assertEqual(read_lease(path)["run_id"], "new-run")

            path.write_text("{ not valid json", encoding="utf-8")
            with self.assertRaises(LeaseHeldError):
                PipelineLease(path, "later-run").acquire()

    def test_pipeline_lease_release_only_removes_its_own_lease(self) -> None:
        from pipeline_core.concurrency import PipelineLease
        from pipeline_core.state import read_lease

        with TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.lock"
            owner = PipelineLease(path, "run-a", pid=os.getpid())
            owner.acquire("T-1")
            PipelineLease(path, "run-b", pid=os.getpid() + 1).release()
            self.assertTrue(path.exists())
            self.assertEqual(read_lease(path)["run_id"], "run-a")
            owner.release()
            self.assertFalse(path.exists())

    def test_pipeline_lease_context_manager_releases_on_exception(self) -> None:
        from pipeline_core.lease import PipelineLease

        with TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.lock"
            with self.assertRaises(RuntimeError):
                with PipelineLease(path, "run-a"):
                    raise RuntimeError("boom")
            self.assertFalse(path.exists())

    def test_mutex_timeout_is_still_a_timeouterror_and_fires_on_a_live_owner(self) -> None:
        from pipeline_core.concurrency import FileMutex, MutexTimeout

        self.assertTrue(issubclass(MutexTimeout, TimeoutError))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "serialized-forge.lock"
            holder = FileMutex(path, run_id="holder")
            holder.acquire()
            try:
                with self.assertRaises(MutexTimeout):
                    FileMutex(path, run_id="other", timeout=0.4).acquire()
            finally:
                holder.release()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
