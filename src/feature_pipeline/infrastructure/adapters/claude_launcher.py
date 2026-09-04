"""Run a built ``claude`` argv through the shared local process runner.

``pipeline_core.adapters.run_subprocess`` used to open its own :class:`subprocess.Popen`. RS-01
routes it here: :class:`ClaudeLauncher` builds a :class:`ProcessSpec` from the launch inputs,
delegates to a :class:`ProcessRunner` (the local runner by default, injectable for tests), and
applies the one Claude-specific touch the generic runner does not — the human-readable
``[adapter] timed out after …`` note appended to stderr on a timeout. The returned
:class:`ProcessOutcome` is translated back to the adapter's ``CompletedProcess`` by the caller,
so the launch request/response contract is unchanged.

Standard library only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import replace

from feature_pipeline.infrastructure.process.runner import LocalProcessRunner
from feature_pipeline.ports.process import ProcessOutcome, ProcessRunner, ProcessSpec


class ClaudeLauncher:
    """Runs one ``claude`` argv as a child process. Holds no CLI-specific translation."""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self._runner: ProcessRunner = runner or LocalProcessRunner()

    def run(
        self,
        argv: Sequence[str],
        *,
        prompt: str = "",
        cwd: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcessOutcome:
        """Launch ``argv`` with ``prompt`` on stdin; raise :class:`ProcessError` if it cannot start."""
        outcome = self._runner.run(
            ProcessSpec(
                argv=tuple(argv),
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                stdin=prompt,
                timeout=timeout,
            )
        )
        if outcome.timed_out:
            note = f"\n[adapter] timed out after {timeout}s; process tree terminated\n"
            outcome = replace(outcome, stderr=outcome.stderr + note)
        return outcome


__all__ = ["ClaudeLauncher"]
