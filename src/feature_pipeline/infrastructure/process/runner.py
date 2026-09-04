"""The one local operating-system process runner.

:class:`LocalProcessRunner` is the single :class:`feature_pipeline.ports.process.ProcessRunner`
implementation. It centralises what the two ``pipeline_core`` call sites each hand-rolled:

* a new process group / session, so the whole tree can be signalled as a unit;
* ``encoding="utf-8"`` with ``errors="strict"`` pinned explicitly on the pipes, never left to
  ``locale.getpreferredencoding()`` (RDS-11 / RDS-12);
* ``stdin`` always a pipe the runner closes — no launch inherits a terminal;
* on timeout: terminate the complete process tree (``taskkill /F /T`` on Windows,
  ``killpg(SIGKILL)`` on POSIX), then drain and keep whatever partial output exists;
* on any other exception mid-run: the same tree termination before the exception propagates,
  so a cancelled launch never leaves a child behind.

``shell=False`` always; argv is a real list. Standard library only.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

from feature_pipeline.ports.process import ProcessError, ProcessOutcome, ProcessSpec

#: How long to wait for a signalled tree to actually exit before giving up on the reap.
KILL_GRACE_S = 10.0


def terminate_process_tree(
    process: "subprocess.Popen[str]", *, grace_s: float = KILL_GRACE_S
) -> None:
    """Kill ``process`` and every descendant so none survives on Windows or POSIX."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            process.kill()
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass


class LocalProcessRunner:
    """Runs a :class:`ProcessSpec` as a real child process (AC-1, AC-2)."""

    def run(self, spec: ProcessSpec) -> ProcessOutcome:
        argv = list(spec.argv)
        if not argv:
            raise ProcessError("refusing to run an empty argv", "empty-argv")

        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
        start = time.monotonic()
        try:
            process: subprocess.Popen[str] = subprocess.Popen(
                argv,
                cwd=spec.cwd,
                env=dict(spec.env) if spec.env is not None else None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=True,
                encoding=spec.encoding,
                errors=spec.errors,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
        except OSError as exc:
            raise ProcessError(
                f"could not start {argv[0]!r}: {exc}", "start-failed"
            ) from None

        try:
            try:
                stdout, stderr = process.communicate(spec.stdin, timeout=spec.timeout)
                return ProcessOutcome(
                    exit_code=process.returncode,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    duration_s=time.monotonic() - start,
                )
            except subprocess.TimeoutExpired as exc:
                terminate_process_tree(process)
                rest_out, rest_err = process.communicate()
                return ProcessOutcome(
                    exit_code=None,
                    stdout=_as_text(exc.stdout) + (rest_out or ""),
                    stderr=_as_text(exc.stderr) + (rest_err or ""),
                    duration_s=time.monotonic() - start,
                    timed_out=True,
                )
        except BaseException:
            terminate_process_tree(process)
            raise


def _as_text(value: object) -> str:
    """Normalise the partial stream ``TimeoutExpired`` carries to text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = ["KILL_GRACE_S", "LocalProcessRunner", "terminate_process_tree"]
