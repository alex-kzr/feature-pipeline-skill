"""The on-disk lock-file record: how a held lease is written and read back.

The JSON keeps every field the pre-RS-02 lock files carried - ``run_id``, ``pid``,
``task_id``, ``started_at`` - so anything that still reads a lock the old way
(``pipeline_core.state.read_lease``) keeps working, and adds two: ``nonce`` (owner identity,
see :mod:`feature_pipeline.infrastructure.locks.owner`) and ``heartbeat_at`` (staleness).

A malformed or unreadable file is reported as :attr:`HolderRecord.unreadable` rather than
raising, so the manager can fail *closed* on it - an ambiguous owner is never deleted.

Standard library only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from feature_pipeline.ports.locks import OwnerToken

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_stamp() -> str:
    """The UTC timestamp spelling the historical lock files use."""
    return datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _parse_stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class HolderRecord:
    """A parsed lock file. ``unreadable`` is set for malformed content, with the other
    fields left at their empty defaults."""

    run_id: str | None = None
    pid: int | None = None
    nonce: str | None = None
    task_id: str | None = None
    acquired_at: str | None = None
    heartbeat_at: datetime | None = None
    unreadable: bool = False

    def as_mapping(self) -> dict[str, object]:
        """The identity fields in the shape :meth:`OwnerToken.matches` expects."""
        return {"run_id": self.run_id, "pid": self.pid, "nonce": self.nonce}


def render_record(
    owner: OwnerToken,
    *,
    task_id: str | None,
    acquired_at: str,
    heartbeat_at: str,
) -> str:
    """Serialize a held lease deterministically (``json.dumps(indent=2)`` + trailing newline)."""
    payload = {
        "run_id": owner.run_id,
        "pid": owner.pid,
        "nonce": owner.nonce,
        "task_id": task_id,
        # ``started_at`` is kept as an alias of ``acquired_at`` purely for readers that were
        # written against the old ``PipelineLease`` format.
        "started_at": acquired_at,
        "acquired_at": acquired_at,
        "heartbeat_at": heartbeat_at,
    }
    return json.dumps(payload, indent=2) + "\n"


def read_holder(path: str | Path) -> HolderRecord | None:
    """Parse the lock at ``path``. ``None`` means "no lock there"; a malformed lock comes
    back with :attr:`HolderRecord.unreadable` set."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return HolderRecord(unreadable=True)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return HolderRecord(unreadable=True)
    if not isinstance(data, dict):
        return HolderRecord(unreadable=True)

    pid = data.get("pid")
    run_id = data.get("run_id")
    nonce = data.get("nonce")
    task_id = data.get("task_id")
    acquired_at = data.get("acquired_at") or data.get("started_at")
    return HolderRecord(
        run_id=run_id if isinstance(run_id, str) else None,
        pid=pid if isinstance(pid, int) else None,
        nonce=nonce if isinstance(nonce, str) else None,
        task_id=task_id if isinstance(task_id, str) else None,
        acquired_at=acquired_at if isinstance(acquired_at, str) else None,
        heartbeat_at=_parse_stamp(data.get("heartbeat_at")),
    )


__all__ = ["HolderRecord", "now_stamp", "read_holder", "render_record"]
