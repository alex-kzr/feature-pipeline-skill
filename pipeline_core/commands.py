"""Safe argv-only command execution with durable, redacted, reproducible evidence.

The runner — never an agent — owns this call. Every launched command produces:

* a stable ``command-N`` record (argv, repo-relative cwd, exit, duration, log paths) persisted
  on the owning :class:`~pipeline_core.state.Run`;
* redacted stdout/stderr logs under ``<run_dir>/logs`` — secrets and machine-local paths are
  removed *before* a byte-aware tail truncation, so a cut can never strip the anchor off a
  path and leave an unrecognizable fragment behind;
* a :class:`CommandOutcome` whose ``disposition`` is ``PASS`` for a clean exit, ``BLOCKED``
  for an unavailable binary / toolchain / launch failure (never spends a repair attempt), and
  ``FAIL`` for a genuine non-zero or timeout.

Output budgets are selected by *outcome*, not by stage: a success keeps the routine 16 KiB
budget, anything else keeps the 64 KiB diagnostic budget. Commands run with ``shell=False``
from a cwd resolved beneath the project root, and the whole process tree is terminated on
timeout or cancellation so no child survives on Windows or POSIX. Every invocation of a
caller-declared serialized program (``serialized_programs``) takes the cross-process write
mutex for that program first.

Every command spawns through the shared
:class:`feature_pipeline.infrastructure.process.runner.LocalProcessRunner` (RS-01), which pins
``encoding="utf-8"`` (with ``errors="strict"``) on the ``subprocess.Popen`` call explicitly —
``text=True`` alone decodes stdout/stderr via ``locale.getpreferredencoding()``, which is not
UTF-8 on every host locale (RDS-12), silently corrupting any non-ASCII byte a verification
command's toolchain writes to its pipes.

Standard library only.
"""

from __future__ import annotations

import os
import shutil
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from feature_pipeline.infrastructure.process import LocalProcessRunner
from feature_pipeline.infrastructure.process.capture import tail_truncate as _tail_truncate
from feature_pipeline.ports.process import ProcessError, ProcessSpec

from .artifacts import write_text_atomic
from .concurrency import is_serialized_program, write_mutex
from .redaction import output_rules, redact_text
from .state import EXIT_LAUNCH_FAILED, EXIT_NOT_FOUND, EXIT_TIMEOUT, Run, repo_relative

ROUTINE_OUTPUT_BUDGET = 16 * 1024
DIAGNOSTIC_OUTPUT_BUDGET = 64 * 1024

DISPOSITION_PASS = "PASS"
DISPOSITION_FAIL = "FAIL"
DISPOSITION_BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class CommandOutcome:
    """The durable evidence one launched command produced."""

    command_id: str
    exit_code: int | str
    disposition: str  # PASS | FAIL | BLOCKED
    stdout_log: str | None
    stderr_log: str | None
    reason: str | None = None

    @property
    def blocked(self) -> bool:
        return self.disposition == DISPOSITION_BLOCKED

    @property
    def passed(self) -> bool:
        return self.disposition == DISPOSITION_PASS


def resolve_program(program: str, cwd: str | Path) -> str | None:
    """Resolve a bare program on PATH or a path-shaped program under its cwd."""
    if os.path.dirname(program):
        candidate = Path(program)
        return shutil.which(str(candidate if candidate.is_absolute() else Path(cwd) / candidate))
    return shutil.which(program)


def classify_outcome(exit_code: int | str) -> tuple[str, str | None]:
    """Map an exit code to ``(disposition, reason)`` deterministically.

    Environmental problems — an absent program, a failed launch — are ``BLOCKED`` so the
    repair loop is never charged for something the change cannot fix. A real non-zero exit or
    a timeout is a ``FAIL``.
    """
    if exit_code == 0:
        return DISPOSITION_PASS, None
    if exit_code == EXIT_NOT_FOUND:
        return DISPOSITION_BLOCKED, "program or toolchain is not available"
    if exit_code == EXIT_LAUNCH_FAILED:
        return DISPOSITION_BLOCKED, "command could not be launched in this environment"
    if exit_code == EXIT_TIMEOUT:
        return DISPOSITION_FAIL, "command exceeded its timeout"
    return DISPOSITION_FAIL, f"command exited with {exit_code}"


def redact_then_truncate(text: str | None, budget: int, repo_root: str | Path) -> str:
    """Redact secrets and machine-local paths, *then* apply the byte budget."""
    return _tail_truncate(redact_text(text or "", output_rules(repo_root)), budget)


def _stage_slug(stage: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in stage)


def run_command(
    run: Run,
    stage: str,
    cwd: str | Path,
    argv: Sequence[str],
    timeout: float | None = None,
    output_limit: int | None = None,
    serialized_programs: Sequence[str] = (),
) -> dict:
    """Run argv without a shell, persist redacted evidence, and record the outcome.

    ``serialized_programs`` is the caller-declared set of program basenames whose invocations
    must not overlap (a compiled-language build tool, typically). When the resolved program is
    one of them the launch is wrapped in its cross-process write mutex; otherwise the set has
    no effect. The core hard-codes no names.
    """
    declared = list(argv)
    if not declared:
        raise ValueError(f"{stage}: refusing to execute an empty command")
    full_cwd = Path(cwd)
    if not full_cwd.is_absolute():
        full_cwd = (run.repo_root / full_cwd).resolve()
    start = time.monotonic()

    if not full_cwd.is_dir():
        return _record(run, stage, full_cwd, declared, EXIT_LAUNCH_FAILED,
                       time.monotonic() - start, "",
                       "working directory does not exist or is not readable", output_limit)
    program = resolve_program(declared[0], full_cwd)
    if not program:
        where = ("not found relative to the command working directory"
                 if os.path.dirname(declared[0]) else "not found on PATH")
        return _record(run, stage, full_cwd, declared, EXIT_NOT_FOUND,
                       time.monotonic() - start, "", f"{declared[0]}: {where}", output_limit)

    mutex = (
        write_mutex(run.repo_root, program, run_id=run.run_id)
        if is_serialized_program(program, serialized_programs)
        else nullcontext()
    )
    try:
        with mutex:
            outcome = LocalProcessRunner().run(ProcessSpec(
                argv=(program, *declared[1:]),
                cwd=str(full_cwd),
                timeout=timeout,
            ))
    except ProcessError as exc:
        return _record(run, stage, full_cwd, declared, EXIT_LAUNCH_FAILED,
                       time.monotonic() - start, "", str(exc), output_limit)

    if outcome.timed_out:
        code: int | str = EXIT_TIMEOUT
    else:
        code = outcome.exit_code if outcome.exit_code is not None else EXIT_LAUNCH_FAILED
    return _record(run, stage, full_cwd, declared, code, time.monotonic() - start,
                   outcome.stdout, outcome.stderr, output_limit)


def _record(
    run: Run,
    stage: str,
    cwd: Path,
    argv: list[str],
    exit_code: int | str,
    duration: float,
    stdout: str,
    stderr: str,
    output_limit: int | None,
) -> dict:
    """Redact, budget, persist logs, and append the stable ``command-N`` record."""
    disposition, reason = classify_outcome(exit_code)
    budget = ROUTINE_OUTPUT_BUDGET if disposition == DISPOSITION_PASS else DIAGNOSTIC_OUTPUT_BUDGET
    if output_limit is not None:
        budget = output_limit
    out_text = redact_then_truncate(stdout, budget, run.repo_root)
    err_text = redact_then_truncate(stderr, budget, run.repo_root)

    logs_dir = Path(run.run_dir) / "logs"
    index = sum(1 for entry in run.commands if entry["stage"] == stage) + 1
    slug = _stage_slug(stage)
    stdout_log = stderr_log = None
    if out_text:
        path = logs_dir / f"{slug}-{index}.stdout.txt"
        write_text_atomic(path, out_text, repo_root=run.repo_root)
        stdout_log = _run_relative(path, run.run_dir)
    if err_text:
        path = logs_dir / f"{slug}-{index}.stderr.txt"
        write_text_atomic(path, err_text, repo_root=run.repo_root)
        stderr_log = _run_relative(path, run.run_dir)

    record = run.record_command(stage, cwd, argv, exit_code, duration, out_text, err_text)
    record["disposition"] = disposition
    record["stdout_log"] = stdout_log
    record["stderr_log"] = stderr_log
    if reason:
        record["reason"] = reason
    return record


def _run_relative(path: Path, run_dir: str | Path) -> str:
    try:
        return path.resolve().relative_to(Path(run_dir).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def verification_stage(task_id: str, *, attempt: int | None = None) -> str:
    """The stable stage key under which a task's verification commands are recorded.

    Every attempt owns its own stage so a repair re-run's evidence never overwrites the
    first pass's records.
    """
    return f"task:{task_id}:verify" if attempt is None else f"task:{task_id}:verify:{attempt}"


def _declared_cwd_argv(command: object) -> tuple[str, tuple[str, ...]]:
    """Read ``(cwd, argv)`` from a :class:`~schemas.contracts.CommandSpec` or a plain mapping."""
    cwd = getattr(command, "cwd", None)
    argv = getattr(command, "argv", None)
    if argv is None and isinstance(command, Mapping):
        cwd = command.get("cwd")
        argv = command.get("argv")
        if argv is None and command.get("command"):
            argv = str(command["command"]).split()
    if argv is None:
        raise ValueError(f"verification command {command!r} carries no argv")
    return (str(cwd) if cwd else "."), tuple(str(part) for part in argv)


@dataclass(frozen=True)
class VerificationRun:
    """The ordered command evidence one verification pass produced.

    ``stopped_reason`` is set when an *external blocker* — a program or toolchain that is not
    available, or a command that could not be launched — halted the pass before every declared
    command ran. ``unrun`` then lists the ``(cwd, argv)`` of the declared checks that were
    deliberately not executed, so the evidence stays explicit about the gap rather than
    looking complete. A real command *failure* (a non-zero exit or a timeout) never stops the
    pass and never appears here.
    """

    records: tuple[dict, ...]
    unrun: tuple[tuple[str, tuple[str, ...]], ...] = ()
    stopped_reason: str | None = None

    @property
    def complete(self) -> bool:
        return self.stopped_reason is None and not self.unrun


def run_verification_commands(
    run: Run,
    commands: Iterable[object],
    *,
    stage: str,
    task_id: str | None = None,
    attempt: int | None = None,
    timeout: float | None = None,
    serialized_programs: Sequence[str] = (),
) -> VerificationRun:
    """Execute every declared verification command in order as runner-owned evidence.

    Each command runs through :func:`run_command` from its own repository-relative working
    directory with a shell-free argv, producing a stable ``command-N`` record on ``run``. A
    command that *fails* does not stop the pass — the complete declared set is still
    exercised. A command that is *blocked* (its program/toolchain is unavailable, or it could
    not be launched) is an external blocker: the pass stops there, the evidence already
    gathered is retained, and the remaining declared checks are returned in ``unrun``.

    ``serialized_programs`` is forwarded verbatim to :func:`run_command` — the caller's set of
    program basenames to hold the write mutex around.
    """
    declared = [_declared_cwd_argv(command) for command in commands]
    records: list[dict] = []
    for index, (cwd, argv) in enumerate(declared, start=1):
        record = run_command(
            run, stage, cwd, list(argv), timeout=timeout,
            serialized_programs=serialized_programs)
        record["command_index"] = index
        if task_id is not None:
            record["task_id"] = task_id
        if attempt is not None:
            record["attempt"] = attempt
        records.append(record)
        if record.get("disposition") == DISPOSITION_BLOCKED:
            reason = record.get("reason") or "declared verification command could not be run"
            return VerificationRun(
                tuple(records),
                tuple(declared[index:]),
                f"{' '.join(argv)}: {reason}",
            )
    return VerificationRun(tuple(records))


def outcome_of(record: dict) -> CommandOutcome:
    """Wrap a persisted ``command-N`` record as a typed :class:`CommandOutcome`."""
    disposition = record.get("disposition")
    if disposition is None:
        disposition, _ = classify_outcome(record["exit_code"])
    return CommandOutcome(
        command_id=record["id"],
        exit_code=record["exit_code"],
        disposition=disposition,
        stdout_log=record.get("stdout_log"),
        stderr_log=record.get("stderr_log"),
        reason=record.get("reason"),
    )
