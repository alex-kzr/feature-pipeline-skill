"""Safe argv-only command execution with portable evidence records."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Sequence

from .state import EXIT_LAUNCH_FAILED, EXIT_NOT_FOUND, EXIT_TIMEOUT, Run


def resolve_program(program: str, cwd: str | Path) -> str | None:
    """Resolve a bare program on PATH or a path-shaped program under its cwd."""
    if os.path.dirname(program):
        candidate = Path(program)
        return shutil.which(str(candidate if candidate.is_absolute() else Path(cwd) / candidate))
    return shutil.which(program)


def run_command(run: Run, stage: str, cwd: str | Path, argv: Sequence[str], timeout: float | None = None, output_limit: int = 16 * 1024) -> dict:
    """Run argv without a shell, cap captured output, and record the outcome."""
    declared = list(argv)
    if not declared:
        raise ValueError(f"{stage}: refusing to execute an empty command")
    full_cwd = Path(cwd)
    if not full_cwd.is_absolute():
        full_cwd = (run.repo_root / full_cwd).resolve()
    start = time.monotonic()
    if not full_cwd.is_dir():
        return run.record_command(stage, full_cwd, declared, EXIT_LAUNCH_FAILED, time.monotonic() - start, "", "working directory does not exist or is not readable")
    program = resolve_program(declared[0], full_cwd)
    if not program:
        return run.record_command(stage, full_cwd, declared, EXIT_NOT_FOUND, time.monotonic() - start, "", "program not found")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen([program, *declared[1:]], cwd=full_cwd, shell=False,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=os.name != "nt", creationflags=creationflags)
        stdout, stderr = process.communicate(timeout=timeout)
        code = process.returncode
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        remaining_out, remaining_err = process.communicate()
        code = EXIT_TIMEOUT
        stdout = (exc.stdout or "") + (remaining_out or "")
        stderr = (exc.stderr or "") + (remaining_err or "")
    except OSError as exc:
        code, stdout, stderr = EXIT_LAUNCH_FAILED, "", str(exc)
    return run.record_command(stage, full_cwd, declared, code, time.monotonic() - start, stdout[:output_limit], stderr[:output_limit])
