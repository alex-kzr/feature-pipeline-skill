"""Cross-process write leases and the build-tool write mutex for write-capable stages.

Two independent guarantees, both enforced with lock files under an explicit project root
(never inferred from the user home) so they hold across separate OS processes:

* **Write leases** - at most one write-capable stage per working tree
  (``docs/.agents/locks/pipeline.lock``) and at most one owner of a given task while its
  executor or repair runs (``docs/.agents/locks/<task-id>.lock``). Read-only verifiers never
  take a lease, which is what keeps concurrent verification safe.
* **Write mutex** - :class:`FileMutex` at ``docs/.agents/locks/serialized-<program>.lock``
  serializes every invocation of a *caller-declared* write-heavy program across *all* stages,
  including otherwise-concurrent verifiers. Overlapping builds that contend on the same output
  files deadlock - the Windows linker's file locks are the canonical case - so any project
  with a compiled-language build tool declares that tool's basename and the runner takes this
  mutex around it. The set of serialized program names is supplied by the caller
  (:func:`pipeline_core.commands.run_command`); nothing is baked in here.

Since RS-02 both the lease and the mutex are thin adapters over the one bounded primitive in
:mod:`feature_pipeline.infrastructure.locks`. An ambiguous owner (a live PID, an unreadable
lock) always blocks; only a demonstrably dead owner is reclaimed. The mutex no longer waits
forever by default - it inherits
:data:`feature_pipeline.ports.locks.DEFAULT_ACQUIRE_TIMEOUT_S` and a caller that truly wants
an unbounded wait passes ``timeout=None`` explicitly. Standard library only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from feature_pipeline.infrastructure.locks.manager import FileLeaseManager
from feature_pipeline.infrastructure.locks.owner import current_owner
from feature_pipeline.ports.locks import (
    DEFAULT_ACQUIRE_TIMEOUT_S,
    DEFAULT_POLL_INTERVAL_S,
    Lease,
    LeaseRequest,
    LeaseTimeout,
)

from .lease import LeaseHeldError, PipelineLease

__all__ = [
    "LeaseHeldError",
    "PipelineLease",
    "MutexTimeout",
    "FileMutex",
    "locks_dir",
    "pipeline_lock_path",
    "task_lock_path",
    "serialized_lock_path",
    "pipeline_lease",
    "task_lease",
    "write_mutex",
    "is_serialized_program",
]

#: Lock directory relative to the project root receiving feature work.
_LOCKS_SUBPATH = ("docs", ".agents", "locks")
#: Per-program write-mutex lock file: ``serialized-<program-basename>.lock``.
SERIALIZED_LOCK_PREFIX = "serialized-"
PIPELINE_LOCK_NAME = "pipeline.lock"


def locks_dir(project_root: str | Path) -> Path:
    """The ``docs/.agents/locks`` directory under an explicit project root."""
    return Path(project_root, *_LOCKS_SUBPATH)


def pipeline_lock_path(project_root: str | Path) -> Path:
    return locks_dir(project_root) / PIPELINE_LOCK_NAME


def task_lock_path(project_root: str | Path, task_id: str) -> Path:
    if not task_id or any(sep in task_id for sep in ("/", "\\", os.sep)) or task_id in {".", ".."}:
        raise ValueError(f"unsafe task id for a lock file: {task_id!r}")
    return locks_dir(project_root) / f"{task_id}.lock"


def _program_slug(program: str | Path) -> str:
    """A filesystem-safe basename for the lock file of one serialized program."""
    stem = Path(program).stem.lower()
    safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in stem)
    return safe or "program"


def serialized_lock_path(project_root: str | Path, program: str | Path) -> Path:
    """The write-mutex lock file for one caller-declared serialized program."""
    return locks_dir(project_root) / f"{SERIALIZED_LOCK_PREFIX}{_program_slug(program)}.lock"


def pipeline_lease(project_root: str | Path, run_id: str, pid: int | None = None) -> PipelineLease:
    """A lease every write-capable stage must hold before it touches the working tree."""
    return PipelineLease(pipeline_lock_path(project_root), run_id, pid)


def task_lease(project_root: str | Path, run_id: str, task_id: str, pid: int | None = None) -> PipelineLease:
    """A lease held while a task's executor or repair window owns the tree."""
    return PipelineLease(task_lock_path(project_root, task_id), run_id, pid)


class MutexTimeout(TimeoutError):
    """A cross-process mutex was not acquired within its (now always bounded) timeout."""


class FileMutex:
    """A mutex held as an exclusive lock file, serializing across independent processes.

    Unlike ``threading.Lock`` this survives the process boundary: the engine launches
    executors and verifiers as separate processes, so write serialization has to live on
    disk. Acquisition is delegated to
    :class:`feature_pipeline.infrastructure.locks.manager.FileLeaseManager`: a lock whose
    recorded PID is proven dead is reclaimed, a live or unreadable owner blocks until it
    frees the lock or ``timeout`` elapses (:class:`MutexTimeout`). ``timeout`` defaults to a
    bounded value; pass ``None`` to wait with no ceiling.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str | None = None,
        pid: int | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        timeout: float | None = DEFAULT_ACQUIRE_TIMEOUT_S,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.pid = pid if pid is not None else os.getpid()
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._owner = current_owner(run_id, pid=self.pid)
        self._manager = FileLeaseManager()
        self._lease: Lease | None = None
        self._held = False

    def acquire(self) -> "FileMutex":
        lease = self._manager.lease(
            LeaseRequest(
                path=str(self.path),
                owner=self._owner,
                acquire_timeout_s=self.timeout,
                poll_interval_s=self.poll_interval,
            )
        )
        try:
            lease.acquire()
        except LeaseTimeout:
            raise MutexTimeout(
                f"write mutex at '{self.path}' was not acquired within {self.timeout}s"
            ) from None
        self._lease = lease
        self._held = True
        return self

    def release(self) -> None:
        if self._lease is not None:
            self._lease.release()
        self._lease = None
        self._held = False

    def __enter__(self) -> "FileMutex":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


def write_mutex(
    project_root: str | Path,
    program: str | Path,
    *,
    run_id: str | None = None,
    pid: int | None = None,
    timeout: float | None = DEFAULT_ACQUIRE_TIMEOUT_S,
) -> FileMutex:
    """The process-wide write mutex serializing one caller-declared program in a project tree.

    ``timeout`` is bounded by default (RS-02 AC-1); pass ``None`` for an unbounded wait.
    """
    return FileMutex(
        serialized_lock_path(project_root, program), run_id=run_id, pid=pid, timeout=timeout)


def is_serialized_program(
    resolved_program: str | Path | None, serialized_programs: Iterable[str | Path] = (),
) -> bool:
    """True when a *resolved* executable's basename is one the caller declared must be serialized.

    ``serialized_programs`` is caller-supplied data - a set of program basenames (a bare name,
    a name with an extension, or a full path are all compared by lowercased stem). Keyed off
    the resolved path rather than the declared ``argv[0]`` so the serialization is a property
    of what actually launches, per RDS-01.
    """
    if not resolved_program:
        return False
    stem = Path(resolved_program).stem.lower()
    declared = {Path(name).stem.lower() for name in (serialized_programs or ())}
    return stem in declared
