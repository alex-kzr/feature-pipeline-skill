"""The bounded-lease / resource-ownership port.

Three call sites in ``pipeline_core`` guard a shared working tree with hand-rolled lock
files: ``pipeline_core.lease.PipelineLease`` (the whole-run write lease and the per-task
lease) and ``pipeline_core.concurrency.FileMutex`` (the serialized-program write mutex).
They agreed on the atomic ``O_CREAT | O_EXCL`` create and on "reclaim only a demonstrably
dead owner", but each re-derived owner identity, staleness, and release safety with subtly
different spellings, and :class:`FileMutex` with no ``timeout`` waited forever.

RS-02 introduces one primitive both depend on:

* :class:`OwnerToken` - who holds a lease. ``run_id`` + ``pid`` were never enough to tell a
  restarted process from the one that crashed and left the lock behind (RS-02 Risks); a
  per-process random :attr:`OwnerToken.nonce` closes that gap, so release and heartbeat can
  fail closed when the PID has been reused.
* :class:`LeaseRequest` - a fully-described acquisition: the lock path, the owner, a
  **bounded** :attr:`~LeaseRequest.acquire_timeout_s` (``None`` is the caller's explicit,
  documented choice to wait unbounded), the poll interval, and the staleness policy.
* :class:`LeaseManager` - hands back a :class:`Lease` (a context manager) for that request.
  Acquisition fails closed on a live or indeterminate owner and reclaims only an owner
  proven dead by the policy.

Nothing here touches the filesystem, the clock, or the environment; the concrete
implementation is :class:`feature_pipeline.infrastructure.locks.manager.FileLeaseManager`
and the current-process owner comes from
:func:`feature_pipeline.infrastructure.locks.owner.current_owner`.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol, runtime_checkable

#: Bounded default wait for :attr:`LeaseRequest.acquire_timeout_s`. Generous enough for a slow
#: serialized build to finish, but never unbounded (RS-02 AC-1). A caller that genuinely wants
#: to wait with no ceiling passes ``acquire_timeout_s=None`` explicitly.
DEFAULT_ACQUIRE_TIMEOUT_S: float = 3600.0

#: How often a bounded acquisition re-checks a held lock while it waits.
DEFAULT_POLL_INTERVAL_S: float = 0.02

#: Age past which a heartbeat is considered stale under :attr:`StalenessPolicy.require_heartbeat`.
DEFAULT_HEARTBEAT_TTL_S: float = 900.0


class LeaseError(RuntimeError):
    """Base for every lease failure. ``code`` is a stable, machine-readable reason."""

    code = "lease-error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class LeaseHeld(LeaseError):
    """The lock is held by a live owner, or by one whose state cannot be resolved.

    Raised immediately when ``acquire_timeout_s`` is ``0`` (the caller wants a verdict, not a
    wait), and after a non-reclaimable owner is still there when a bounded wait would rather
    report "held" than "timed out" - an unreadable lock, or a record with no PID.
    """

    code = "lease-held"


class LeaseTimeout(LeaseError, TimeoutError):
    """A bounded acquisition did not obtain the lock before ``acquire_timeout_s`` elapsed."""

    code = "lease-timeout"


class NotLeaseOwner(LeaseError):
    """A heartbeat or an owner-only operation was attempted against a lock this token does
    not own - typically because the PID was reused by an unrelated process."""

    code = "not-lease-owner"


@dataclass(frozen=True)
class OwnerToken:
    """The identity of one lease holder.

    ``run_id`` is the logical owner (``None`` for the process-wide write mutex, which is not
    scoped to a run). ``pid`` is the OS process. ``nonce`` is a value minted once per process
    start, so two runs of the same executable that happen to reuse a PID are still distinct
    owners.
    """

    run_id: str | None
    pid: int
    nonce: str

    def as_record(self) -> dict[str, object]:
        """The owner fields as they are written into a lock file."""
        return {"run_id": self.run_id, "pid": self.pid, "nonce": self.nonce}

    def matches(self, record: Mapping[str, object]) -> bool:
        """True when ``record`` describes *this* owner.

        The PID must match, and the ``run_id`` must match unless this token's ``run_id`` is
        ``None``. The ``nonce`` must match too, except against a legacy record written before
        RS-02 (no ``nonce`` key), where PID + ``run_id`` is the strongest evidence available
        and is accepted so an in-place upgrade can still release its own old lock.
        """
        if record.get("pid") != self.pid:
            return False
        if self.run_id is not None and record.get("run_id") != self.run_id:
            return False
        recorded_nonce = record.get("nonce")
        if recorded_nonce is None:
            return True
        return recorded_nonce == self.nonce


@dataclass(frozen=True)
class StalenessPolicy:
    """When a held lock may be taken from its recorded owner.

    A demonstrably dead PID is always reclaimable. ``require_heartbeat`` additionally lets a
    caller that *does* refresh its lease (:meth:`Lease.touch`) reclaim a lock whose heartbeat
    is older than ``heartbeat_ttl_s`` even when the PID still appears alive - the PID-reuse
    case. The migrated ``pipeline_core`` call sites do not heartbeat, so they leave it off and
    keep the historical "proven-dead only" behaviour byte for byte.
    """

    heartbeat_ttl_s: float = DEFAULT_HEARTBEAT_TTL_S
    require_heartbeat: bool = False


@dataclass(frozen=True)
class LeaseRequest:
    """One fully-described request to hold a lock file."""

    path: str
    owner: OwnerToken
    acquire_timeout_s: float | None = DEFAULT_ACQUIRE_TIMEOUT_S
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    staleness: StalenessPolicy = field(default_factory=StalenessPolicy)
    task_id: str | None = None


@runtime_checkable
class Lease(Protocol):
    """A held (or holdable) lock, usable directly or as a context manager."""

    @property
    def held(self) -> bool:
        """True between a successful :meth:`acquire` and the matching :meth:`release`."""
        ...

    def acquire(self) -> "Lease":
        """Take the lock, or raise :class:`LeaseHeld` / :class:`LeaseTimeout`."""
        ...

    def release(self) -> None:
        """Remove the lock **iff** it is still owned by this token. Never raises."""
        ...

    def touch(self) -> None:
        """Refresh this owner's heartbeat. Raises :class:`NotLeaseOwner` if not the owner."""
        ...

    def __enter__(self) -> "Lease":
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        ...


@runtime_checkable
class LeaseManager(Protocol):
    """Builds a :class:`Lease` for a :class:`LeaseRequest`."""

    def lease(self, request: LeaseRequest) -> Lease:
        ...


__all__ = [
    "DEFAULT_ACQUIRE_TIMEOUT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_HEARTBEAT_TTL_S",
    "LeaseError",
    "LeaseHeld",
    "LeaseTimeout",
    "NotLeaseOwner",
    "OwnerToken",
    "StalenessPolicy",
    "LeaseRequest",
    "Lease",
    "LeaseManager",
]
