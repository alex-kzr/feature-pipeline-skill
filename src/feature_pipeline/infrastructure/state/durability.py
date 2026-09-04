"""The ADR-005 crash-safe write primitive: unique temp, ``fsync``, ``os.replace``.

**DS-03** (``docs/plans/tasks/DS-03_transactional-state-repository.md``).

``pipeline_core.artifacts.write_json_atomic`` / ``write_text_atomic`` use a *fixed*
``<name>.tmp`` neighbour (collides across concurrent writers, finding F-17), never ``fsync``
(a rename can still lose data on power loss, ``docs/adr/005`` rejected alternative), and run
the payload through redaction (finding F-18 — authoritative bytes get silently rewritten).

:func:`atomic_write` fixes all three:

1. serialize is the caller's job — this function only takes ``bytes``; nothing is redacted;
2. a **unique** temp file ``<name>.<pid>.<hex>.tmp`` in the *target directory*;
3. ``write`` -> ``flush`` -> :func:`os.fsync` the file, then :func:`os.replace`, then a
   best-effort :func:`os.fsync` of the containing directory.

The directory ``fsync`` is POSIX-only; on Windows :func:`os.open` cannot open a directory and
the call is skipped. That platform difference is documented here, not papered over
(``docs/adr/005`` step 3).

Standard library only.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def fsync_dir(directory: Path) -> bool:
    """``fsync`` ``directory`` so a rename into it is durable. Returns whether it ran.

    On Windows a directory handle cannot be obtained this way; the function returns
    ``False`` and the caller relies on :func:`os.replace` atomicity alone, exactly as
    ``docs/adr/005`` step 3 describes.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except (OSError, ValueError):
        return False
    try:
        os.fsync(fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def unique_temp_name(target: Path) -> Path:
    """A collision-free sibling temp path for ``target`` (``<name>.<pid>.<hex>.tmp``)."""
    return target.with_name(f"{target.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")


def atomic_write(target: str | os.PathLike[str], data: bytes, *, fsync: bool = True) -> Path:
    """Publish ``data`` at ``target`` atomically and (by default) durably.

    A unique temp neighbour is written, flushed and ``fsync``-ed, then :func:`os.replace`-d
    onto ``target``; the parent directory is then ``fsync``-ed on POSIX. ``fsync=False``
    keeps the atomic rename but skips the flushes — for tests and throwaway scratch only.
    The temp file is removed if the publish raises before the rename.
    """
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_temp_name(path)
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if fsync:
        fsync_dir(path.parent)
    return path


def atomic_write_text(
    target: str | os.PathLike[str], text: str, *, fsync: bool = True
) -> Path:
    """:func:`atomic_write` for UTF-8 text. No redaction — see the module docstring."""
    return atomic_write(target, text.encode("utf-8"), fsync=fsync)


__all__ = [
    "atomic_write",
    "atomic_write_text",
    "fsync_dir",
    "unique_temp_name",
]
