"""The one file-backed bounded-lease implementation.

:class:`FileLeaseManager` is the single
:class:`feature_pipeline.ports.locks.LeaseManager`. Every lease it hands out:

* acquires with an atomic ``O_CREAT | O_EXCL`` create, the same primitive the three
  hand-rolled call sites used;
* **bounds** the wait - ``acquire_timeout_s=0`` returns a verdict at once
  (:class:`LeaseHeld`), a positive value polls until a deadline then raises
  :class:`LeaseTimeout`, and only an explicit ``None`` waits with no ceiling;
* reclaims a held lock **only** when the recorded owner is proven dead, or - under
  :attr:`StalenessPolicy.require_heartbeat` - when its heartbeat has aged out; an unreadable
  lock or one with no PID always fails closed;
* releases race-safely: the lock is removed only if the on-disk record still matches this
  :class:`OwnerToken` (PID *and* nonce), so a reused PID cannot delete someone else's lease;
* is a context manager whose ``__exit__`` always releases - on success, exception, or a
  cancelling ``BaseException``.

Standard library only.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Callable

from feature_pipeline.infrastructure.locks.owner import pid_alive
from feature_pipeline.infrastructure.locks.record import (
    HolderRecord,
    now_stamp,
    read_holder,
    render_record,
)
from feature_pipeline.ports.locks import (
    Lease,
    LeaseHeld,
    LeaseRequest,
    LeaseTimeout,
    NotLeaseOwner,
)

#: Ceiling on free/recreate races for a zero-timeout acquisition, so even a pathological
#: contender loop terminates with a verdict instead of spinning forever.
_MAX_ZERO_TIMEOUT_SPINS = 10_000


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class _FileLease:
    """One acquirable lock file. Created by :meth:`FileLeaseManager.lease`."""

    def __init__(self, request: LeaseRequest, monotonic: Callable[[], float]) -> None:
        self._request = request
        self._path = Path(request.path)
        self._owner = request.owner
        self._monotonic = monotonic
        self._held = False

    # -- protocol ---------------------------------------------------------------------

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> "_FileLease":
        if self._held:
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        timeout = self._request.acquire_timeout_s
        deadline: float | None = None
        if timeout is not None and timeout > 0:
            deadline = self._monotonic() + timeout

        spins = 0
        while True:
            try:
                descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                record = read_holder(self._path)
                if record is None:
                    # Freed between the failed create and the read - retry the create.
                    spins = self._bump_spins(spins)
                    continue
                if self._reclaimable(record):
                    _unlink_quietly(self._path)
                    spins = self._bump_spins(spins)
                    continue
                # An unreadable or PID-less lock will never resolve itself - waiting on it
                # only delays the same "held" verdict, so fail closed at once.
                if record.unreadable or record.pid is None:
                    raise LeaseHeld(self._held_message(record))
                # A live owner may still exit: honour a bounded wait, or report at once.
                if timeout == 0:
                    raise LeaseHeld(self._held_message(record))
                if deadline is not None and self._monotonic() >= deadline:
                    raise LeaseTimeout(
                        f"lock at '{self._path}' was not acquired within "
                        f"{self._request.acquire_timeout_s}s"
                    )
                time.sleep(self._request.poll_interval_s)
                continue

            stamp = now_stamp()
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    render_record(
                        self._owner,
                        task_id=self._request.task_id,
                        acquired_at=stamp,
                        heartbeat_at=stamp,
                    )
                )
            self._held = True
            return self

    def release(self) -> None:
        record = read_holder(self._path)
        if record is not None and self._owner.matches(record.as_mapping()):
            _unlink_quietly(self._path)
        self._held = False

    def touch(self) -> None:
        record = read_holder(self._path)
        if record is None or not self._owner.matches(record.as_mapping()):
            raise NotLeaseOwner(
                f"cannot refresh a lock at '{self._path}' this token does not own"
            )
        stamp = now_stamp()
        rendered = render_record(
            self._owner,
            task_id=record.task_id,
            acquired_at=record.acquired_at or stamp,
            heartbeat_at=stamp,
        )
        temp = self._path.with_name(f"{self._path.name}.{os.getpid()}.heartbeat.tmp")
        temp.write_text(rendered, encoding="utf-8")
        os.replace(temp, self._path)
        self._held = True

    def __enter__(self) -> "_FileLease":
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    # -- internals ------------------------------------------------------------------------

    def _bump_spins(self, spins: int) -> int:
        spins += 1
        if self._request.acquire_timeout_s == 0 and spins > _MAX_ZERO_TIMEOUT_SPINS:
            raise LeaseHeld(
                f"lock at '{self._path}' is under sustained contention and could not be taken"
            )
        return spins

    def _reclaimable(self, record: HolderRecord) -> bool:
        if record.unreadable or record.pid is None:
            return False
        if not pid_alive(record.pid):
            return True
        policy = self._request.staleness
        if policy.require_heartbeat and record.heartbeat_at is not None:
            age = (datetime.now(timezone.utc) - record.heartbeat_at).total_seconds()
            if age > policy.heartbeat_ttl_s:
                return True
        return False

    def _held_message(self, record: HolderRecord) -> str:
        if record.unreadable:
            return f"lock at '{self._path}' is unreadable and its owner cannot be resolved"
        if record.pid is None:
            return f"lock at '{self._path}' records no owning process"
        return (
            f"lock at '{self._path}' is held by pid {record.pid} "
            f"(run {record.run_id!r}, task {record.task_id!r})"
        )


class FileLeaseManager:
    """Hands out one :class:`_FileLease` per :class:`LeaseRequest`.

    Structurally a :class:`feature_pipeline.ports.locks.LeaseManager`.
    """

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic

    def lease(self, request: LeaseRequest) -> Lease:
        return _FileLease(request, self._monotonic)


__all__ = ["FileLeaseManager"]
