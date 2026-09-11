"""Immutable producer identities for task-scoped pipeline launches."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkItem:
    """The registered producer permitted to launch work for one task in one run."""

    run_id: str
    task_id: str

    @property
    def producer_id(self) -> str:
        return f"{self.run_id}:{self.task_id}"


__all__ = ["WorkItem"]
