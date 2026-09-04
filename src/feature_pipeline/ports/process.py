"""The operating-system process port.

Two call sites in ``pipeline_core`` each open their own :class:`subprocess.Popen`:
``pipeline_core.adapters.run_subprocess`` (the Claude launch) and
``pipeline_core.commands.run_command`` (a task's verification commands). They duplicated
process-group creation, UTF-8 pinning, timeout handling, and process-tree termination with
subtly different spellings.

RS-01 introduces one narrow port — :class:`ProcessRunner` — that both sites depend on instead
of calling :mod:`subprocess` directly. A request is a fully-described :class:`ProcessSpec`
(argv, working directory, environment, stdin text, timeout); the result is a structured
:class:`ProcessOutcome` (exit code, both captured streams, a ``timed_out`` flag, wall
duration). No launch inherits a terminal and none waits without a bound: stdin is always a
pipe the runner closes, and a spec with no timeout is the caller's explicit choice.

Nothing here reads the wall clock, the filesystem, or the environment except through a spec a
caller built. The concrete implementation is
:class:`feature_pipeline.infrastructure.process.runner.LocalProcessRunner`.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: Conventional timeout exit status shared with ``pipeline_core.adapters``. A caller that keeps
#: integer exit codes uses this; a caller that keeps string sentinels reads ``timed_out``.
EXIT_TIMEOUT = 124


class ProcessError(RuntimeError):
    """A process that could not be started. ``code`` is a stable, machine-readable reason.

    A process that started and then failed, timed out, or was cancelled is a normal
    :class:`ProcessOutcome`, never this exception.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProcessSpec:
    """One fully-described request to run an operating-system process.

    ``env`` of ``None`` means "inherit this process's environment"; a mapping fully replaces
    it. ``stdin`` is written to the child and the pipe is then closed; ``None`` closes it
    immediately so a child that reads stdin sees EOF rather than blocking on an inherited
    terminal. ``timeout`` of ``None`` means the caller has deliberately chosen an unbounded
    wait.
    """

    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    stdin: str | None = None
    timeout: float | None = None
    encoding: str = "utf-8"
    errors: str = "strict"

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(str(part) for part in self.argv))


@dataclass(frozen=True)
class ProcessOutcome:
    """The evidence one launched process produced. Facts only — no interpretation.

    ``exit_code`` is the real return code, or ``None`` when the process was killed on timeout
    or cancellation (``timed_out`` then distinguishes the two for a caller that cares).
    ``stdout`` / ``stderr`` carry whatever was captured, including partial output drained after
    a kill.
    """

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@runtime_checkable
class ProcessRunner(Protocol):
    """Runs one :class:`ProcessSpec` to completion and returns its :class:`ProcessOutcome`."""

    def run(self, spec: ProcessSpec) -> ProcessOutcome:
        """Launch ``spec`` with no shell, capture both streams, and return the outcome.

        Raises :class:`ProcessError` only when the process could not be started at all.
        """
        ...


__all__ = [
    "EXIT_TIMEOUT",
    "ProcessError",
    "ProcessOutcome",
    "ProcessRunner",
    "ProcessSpec",
]
