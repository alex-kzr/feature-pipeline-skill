"""One bounded owner-token lease behind every ``pipeline_core`` resource lock (RS-02).

:class:`~feature_pipeline.infrastructure.locks.manager.FileLeaseManager` is the single
implementation of :class:`feature_pipeline.ports.locks.LeaseManager`;
``pipeline_core.lease.PipelineLease`` (the run and per-task write leases) and
``pipeline_core.concurrency.FileMutex`` (the serialized-program write mutex) both delegate to
it. :func:`~feature_pipeline.infrastructure.locks.owner.current_owner` mints the per-process
:class:`~feature_pipeline.ports.locks.OwnerToken` that survives PID reuse.

Standard library only.
"""

from __future__ import annotations

from .manager import FileLeaseManager
from .owner import PROCESS_NONCE, current_owner, pid_alive
from .record import HolderRecord, now_stamp, read_holder, render_record

__all__ = [
    "FileLeaseManager",
    "PROCESS_NONCE",
    "current_owner",
    "pid_alive",
    "HolderRecord",
    "now_stamp",
    "read_holder",
    "render_record",
]
