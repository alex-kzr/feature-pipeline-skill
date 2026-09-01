"""Exclusive lease for write-capable portable stages.

Exactly one write-capable process may hold a given lease at a time. Acquisition is an atomic
``O_CREAT | O_EXCL`` create; when the file already exists the lease is reclaimed only if its
recorded PID is *proven dead*. A live owner, an unreadable lease, or a PID whose liveness
cannot be determined all fail closed — the caller is told the lease is held rather than
having an ambiguous owner deleted. Release only ever removes a lease still owned by this
run and PID, including on the exception path out of the context manager.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .state import pid_alive, read_lease


class LeaseHeldError(Exception):
    """Another live (or indeterminate) writer owns the requested lease."""

    def __init__(self, message: str, code: str = "lease-held") -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PipelineLease:
    """Acquire via atomic creation and reclaim only demonstrably stale leases."""

    def __init__(self, path: str | Path, run_id: str, pid: int | None = None) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.pid = pid if pid is not None else os.getpid()
        self._held_for: str | None = None
        self._held = False

    def acquire(self, task_id: str | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "pid": self.pid,
            "task_id": task_id,
            "started_at": _now(),
        }
        while True:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                held = read_lease(self.path)
                if held is None:
                    # Freed between the failed create and the read — retry the create.
                    continue
                if held.get("unreadable") or held.get("pid") is None or pid_alive(held.get("pid")):
                    raise LeaseHeldError(
                        f"write lease at '{self.path}' is held by pid {held.get('pid')} "
                        f"(task {held.get('task_id')!r})")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise LeaseHeldError(
                        f"stale write lease at '{self.path}' could not be reclaimed: {exc}"
                    ) from None
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2) + "\n")
            self._held_for = task_id
            self._held = True
            return

    def release(self) -> None:
        held = read_lease(self.path)
        if held and held.get("run_id") == self.run_id and held.get("pid") == self.pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._held_for = None
        self._held = False

    def __enter__(self) -> "PipelineLease":
        self.acquire(self._held_for)
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
