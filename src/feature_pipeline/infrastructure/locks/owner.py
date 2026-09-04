"""Current-process owner identity and a signal-free PID liveness probe.

The :data:`PROCESS_NONCE` is minted once when this module is first imported. Every
:class:`~feature_pipeline.ports.locks.OwnerToken` built by :func:`current_owner` in the same
process carries that value, so a lock left behind by a crashed process is not mistaken for
one held by a later process that reused the PID (RS-02 Risks).

Standard library only.
"""

from __future__ import annotations

import os
import secrets

from feature_pipeline.ports.locks import OwnerToken

#: Distinct per process start. Not persisted anywhere except inside a live lock file.
PROCESS_NONCE: str = secrets.token_hex(8)


def current_owner(run_id: str | None, *, pid: int | None = None) -> OwnerToken:
    """The :class:`OwnerToken` for this process (optionally overriding the PID in tests)."""
    return OwnerToken(
        run_id=run_id,
        pid=pid if pid is not None else os.getpid(),
        nonce=PROCESS_NONCE,
    )


def pid_alive(pid: int | None) -> bool:
    """Probe liveness without signalling the process.

    Mirrors ``pipeline_core.state.pid_alive``: ``None`` is treated as "cannot tell, assume
    alive" (fail closed), a non-positive PID is dead, and an access-denied probe on POSIX
    means the PID exists.
    """
    if pid is None:
        return True
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = ["PROCESS_NONCE", "current_owner", "pid_alive"]
