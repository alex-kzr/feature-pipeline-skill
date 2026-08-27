"""Exclusive lease for write-capable portable stages."""

from __future__ import annotations

import os
from pathlib import Path

import json
from datetime import datetime, timezone

from .state import pid_alive, read_lease


class LeaseHeldError(Exception):
    """Another live writer owns the requested lease."""


class PipelineLease:
    """Acquire via atomic creation and reclaim only demonstrably stale leases."""
    def __init__(self, path: str | Path, run_id: str, pid: int | None = None) -> None:
        self.path, self.run_id, self.pid = Path(path), run_id, pid or os.getpid()

    def acquire(self, task_id: str | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = read_lease(self.path)
            if held is None or pid_alive(held.get("pid")):
                raise LeaseHeldError(f"write lease at '{self.path}' is held")
            self.path.unlink()
            return self.acquire(task_id)
        payload = {"run_id": self.run_id, "pid": self.pid, "task_id": task_id,
                   "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")

    def release(self) -> None:
        held = read_lease(self.path)
        if held and held.get("run_id") == self.run_id and held.get("pid") == self.pid:
            self.path.unlink()

    def __enter__(self) -> "PipelineLease":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
