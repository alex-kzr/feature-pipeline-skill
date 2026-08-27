"""Tool-neutral launch contracts for pipeline roles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class LaunchRequest:
    """One tool-independent request to run a pipeline role."""

    role: str
    task_id: str
    prompt: str
    report_path: Path
    read_only: bool = False


@dataclass(frozen=True)
class LaunchResult:
    """The evidence returned by one adapter launch."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    session_id: str | None = None


class Adapter(Protocol):
    """A CLI-specific implementation of a role launch."""

    def launch(self, request: LaunchRequest) -> LaunchResult:
        """Launch ``request`` and return its captured result."""
