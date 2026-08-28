"""Generic deterministic stages driven entirely by project-declared data.

A stage is described by a :class:`schemas.ToolStage`: an argv, the logical paths it needs
present before it runs (``prerequisites``), the logical paths it must produce (``outputs``), an
optional timeout, and a ``diff_policy``. :func:`run_tool_stage` executes that description the
same way for every project — there is no branch on project identity, tool name, or route here.

The release dry run (:func:`plan_release_dry_run`) is the same contract with the execution
removed: it returns the planned commands and never runs, stages, commits, or pushes anything.
Release topology — which repositories, in which order, with which messages — is a project
concern and stays out of this module.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from schemas import SchemaError, ToolStage

from .commands import run_command
from .state import EXIT_NOT_FOUND, EXIT_TIMEOUT, Run

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"

#: argv verbs a dry run must never even plan — it produces a plan, never a mutation.
FORBIDDEN_RELEASE_VERBS = frozenset({"push", "commit", "tag"})

CommandRunner = Callable[..., Mapping[str, object]]
TrackedChangesProbe = Callable[[], Iterable[str]]


@dataclass(frozen=True)
class StageOutcome:
    """The evidence one generic stage produced, with a fail-closed verdict."""

    name: str
    verdict: str
    command: Mapping[str, object] | None
    missing_prerequisites: tuple[str, ...] = ()
    missing_outputs: tuple[str, ...] = ()
    unexpected_tracked_changes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedCommand:
    """One declared release step, resolved to a working directory and argv but not executed."""

    name: str
    cwd: str
    argv: tuple[str, ...]
    prerequisites: tuple[str, ...]
    outputs: tuple[str, ...]
    timeout: float | None
    diff_policy: str


def run_tool_stage(
    run: Run,
    stage: ToolStage,
    root: str | Path,
    *,
    tracked_changes: TrackedChangesProbe | None = None,
    command_runner: CommandRunner = run_command,
) -> StageOutcome:
    """Run one project-declared stage and return its verdict.

    ``BLOCKED`` means the stage could not be judged (a prerequisite is absent, the program is
    not on PATH, a timeout fired, or a ``tracked-empty`` policy was declared without a probe to
    check it). ``FAIL`` means it ran and did not meet its declared contract. Neither degrades
    into a warning.
    """
    base = Path(root)
    missing_prerequisites = tuple(p for p in stage.prerequisites if not (base / p).exists())
    if missing_prerequisites:
        return StageOutcome(
            stage.name, BLOCKED, None, missing_prerequisites=missing_prerequisites,
            reasons=(f"prerequisites absent: {', '.join(missing_prerequisites)}",),
        )

    if stage.diff_policy == "tracked-empty" and tracked_changes is None:
        return StageOutcome(
            stage.name, BLOCKED, None,
            reasons=("diff_policy 'tracked-empty' needs a tracked_changes probe",),
        )
    before = frozenset(tracked_changes()) if tracked_changes is not None else frozenset()

    record = command_runner(run, f"stage:{stage.name}", base, stage.argv, timeout=stage.timeout)
    exit_code = record["exit_code"]

    reasons: list[str] = []
    verdict = PASS
    if exit_code in (EXIT_NOT_FOUND, EXIT_TIMEOUT):
        verdict = BLOCKED
        reasons.append(f"command could not complete (exit {exit_code})")
    elif exit_code != 0:
        verdict = FAIL
        reasons.append(f"command exited with {exit_code}")

    missing_outputs = tuple(p for p in stage.outputs if not (base / p).exists())
    if missing_outputs and verdict != BLOCKED:
        verdict = FAIL
        reasons.append(f"declared outputs absent: {', '.join(missing_outputs)}")

    unexpected: tuple[str, ...] = ()
    if stage.diff_policy == "tracked-empty" and tracked_changes is not None:
        unexpected = tuple(sorted(frozenset(tracked_changes()) - before))
        if unexpected and verdict != BLOCKED:
            verdict = FAIL
            reasons.append(f"unexpected tracked changes: {', '.join(unexpected)}")

    return StageOutcome(
        stage.name, verdict, record,
        missing_prerequisites=(), missing_outputs=missing_outputs,
        unexpected_tracked_changes=unexpected, reasons=tuple(reasons),
    )


def plan_release_dry_run(stages: Sequence[ToolStage], root: str | Path) -> tuple[PlannedCommand, ...]:
    """Resolve declared release steps to planned commands without executing anything.

    Raises :class:`schemas.SchemaError` if a step would plan a mutating verb — a dry run that
    could push or commit is not a dry run.
    """
    base = Path(root)
    planned: list[PlannedCommand] = []
    for stage in stages:
        lowered = {token.lower() for token in stage.argv}
        forbidden = lowered & FORBIDDEN_RELEASE_VERBS
        if forbidden:
            raise SchemaError(
                f"release dry run cannot plan a mutating verb: {', '.join(sorted(forbidden))}"
            )
        planned.append(PlannedCommand(
            name=stage.name,
            cwd=str(base),
            argv=stage.argv,
            prerequisites=stage.prerequisites,
            outputs=stage.outputs,
            timeout=stage.timeout,
            diff_policy=stage.diff_policy,
        ))
    return tuple(planned)
