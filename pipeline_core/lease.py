"""Exclusive lease for write-capable portable stages.

Exactly one write-capable process may hold a given lease at a time. Since RS-02 the mechanism
is the one bounded primitive in :mod:`feature_pipeline.infrastructure.locks` - an atomic
``O_CREAT | O_EXCL`` create, an owner token (run id + PID + a per-process nonce, so a reused
PID cannot pass for the process that crashed), and reclaim only of an owner *proven dead*. A
live owner, an unreadable lease, or a PID whose liveness cannot be determined all fail closed
with :class:`LeaseHeldError` rather than having an ambiguous owner deleted. Release only ever
removes a lease still owned by this token, including on the exception path out of the context
manager.
"""

from __future__ import annotations

import os
from pathlib import Path

from feature_pipeline.infrastructure.locks.manager import FileLeaseManager
from feature_pipeline.infrastructure.locks.owner import current_owner
from feature_pipeline.ports.locks import Lease, LeaseHeld, LeaseRequest, LeaseTimeout


class LeaseHeldError(Exception):
    """Another live (or indeterminate) writer owns the requested lease."""

    def __init__(self, message: str, code: str = "lease-held") -> None:
        super().__init__(message)
        self.code = code


class PipelineLease:
    """Acquire via the shared bounded lease and reclaim only demonstrably stale owners.

    ``acquire`` asks for an immediate verdict (``acquire_timeout_s=0``): a live or ambiguous
    owner raises :class:`LeaseHeldError` straight away, matching the historical semantics
    where the caller is told the lease is held rather than made to wait.
    """

    def __init__(self, path: str | Path, run_id: str, pid: int | None = None) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.pid = pid if pid is not None else os.getpid()
        self._owner = current_owner(run_id, pid=self.pid)
        self._manager = FileLeaseManager()
        self._lease: Lease | None = None
        self._held_for: str | None = None
        self._held = False

    def _new_lease(self, task_id: str | None) -> Lease:
        return self._manager.lease(
            LeaseRequest(
                path=str(self.path),
                owner=self._owner,
                acquire_timeout_s=0,
                task_id=task_id,
            )
        )

    def acquire(self, task_id: str | None = None) -> None:
        lease = self._new_lease(task_id)
        try:
            lease.acquire()
        except (LeaseHeld, LeaseTimeout) as exc:
            raise LeaseHeldError(str(exc), getattr(exc, "code", "lease-held")) from None
        self._lease = lease
        self._held_for = task_id
        self._held = True

    def release(self) -> None:
        lease = self._lease if self._lease is not None else self._new_lease(self._held_for)
        lease.release()
        self._lease = None
        self._held_for = None
        self._held = False

    def __enter__(self) -> "PipelineLease":
        self.acquire(self._held_for)
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
