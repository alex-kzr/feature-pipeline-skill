"""Run a built ``codex exec`` argv through the shared local process runner."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import replace

from feature_pipeline.infrastructure.process.runner import LocalProcessRunner
from feature_pipeline.ports.process import ProcessOutcome, ProcessRunner, ProcessSpec


class CodexLauncher:
    """Runs one ``codex exec`` argv as a child process without interpreting its output."""

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


__all__ = ["CodexLauncher"]
