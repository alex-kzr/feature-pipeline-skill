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
timeout or cancellation so no child survives on Windows or POSIX. Every ``cargo`` invocation
takes the cross-process Cargo mutex first.

Standard library only.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .artifacts import write_text_atomic
from .concurrency import cargo_mutex, is_cargo_program
from .redaction import output_rules, redact_text
from .state import EXIT_LAUNCH_FAILED, EXIT_NOT_FOUND, EXIT_TIMEOUT, Run, repo_relative

ROUTINE_OUTPUT_BUDGET = 16 * 1024
DIAGNOSTIC_OUTPUT_BUDGET = 64 * 1024

DISPOSITION_PASS = "PASS"
DISPOSITION_FAIL = "FAIL"
DISPOSITION_BLOCKED = "BLOCKED"

_TRUNCATION_MARKER = "[truncated]"


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


def _tail_truncate(text: str, budget: int) -> str:
    """Keep the last ``budget`` bytes of UTF-8 text with an explicit dropped-bytes header."""
    encoded = (text or "").encode("utf-8", errors="replace")
    if len(encoded) <= budget:
        return text or ""
    if budget <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:budget]
    tail_budget = budget
    while True:
        dropped = len(encoded) - tail_budget
        header = f"[{dropped} bytes truncated; showing the last {tail_budget}]\n"
        header_size = len(header.encode("utf-8"))
        if header_size + tail_budget <= budget:
            return header + encoded[-tail_budget:].decode("utf-8", errors="ignore")
        resolved_tail = budget - header_size
        if resolved_tail <= 0:
            return _TRUNCATION_MARKER
        tail_budget = min(tail_budget - 1, resolved_tail)


def redact_then_truncate(text: str | None, budget: int, repo_root: str | Path) -> str:
    """Redact secrets and machine-local paths, *then* apply the byte budget."""
    return _tail_truncate(redact_text(text or "", output_rules(repo_root)), budget)


def _stage_slug(stage: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in stage)


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Terminate the whole tree so no child or grandchild survives (AC-3)."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()


def run_command(
    run: Run,
    stage: str,
    cwd: str | Path,
    argv: Sequence[str],
    timeout: float | None = None,
    output_limit: int | None = None,
) -> dict:
    """Run argv without a shell, persist redacted evidence, and record the outcome."""
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

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    mutex = cargo_mutex(run.repo_root, run_id=run.run_id) if is_cargo_program(program) else nullcontext()
    process: subprocess.Popen | None = None
    try:
        with mutex:
            process = subprocess.Popen(
                [program, *declared[1:]], cwd=full_cwd, shell=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=os.name != "nt", creationflags=creationflags)
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                code: int | str = process.returncode
            except subprocess.TimeoutExpired as exc:
                _kill_process_tree(process)
                remaining_out, remaining_err = process.communicate()
                code = EXIT_TIMEOUT
                stdout = (exc.stdout or "") + (remaining_out or "")
                stderr = (exc.stderr or "") + (remaining_err or "")
    except OSError as exc:
        return _record(run, stage, full_cwd, declared, EXIT_LAUNCH_FAILED,
                       time.monotonic() - start, "", str(exc), output_limit)
    except BaseException:
        if process is not None:
            _kill_process_tree(process)
        raise
    return _record(run, stage, full_cwd, declared, code, time.monotonic() - start,
                   stdout, stderr, output_limit)


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
