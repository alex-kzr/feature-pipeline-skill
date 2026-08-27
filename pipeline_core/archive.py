"""Read-only, digest-bound archive manifest primitives."""

from __future__ import annotations

import hashlib
from pathlib import Path


class ArchiveError(ValueError):
    """An unsafe or stale archive request."""


def archive_manifest(root: str | Path, paths: list[str]) -> dict[str, object]:
    """Build a read-only archive manifest for regular files below ``root``."""
    base = Path(root).resolve()
    entries: list[dict[str, object]] = []
    for relative in paths:
        candidate = (base / relative).resolve()
        if candidate == base or base not in candidate.parents or candidate.is_symlink() or not candidate.is_file():
            raise ArchiveError("archive target must be a regular file inside the archive root")
        data = candidate.read_bytes()
        entries.append({"path": candidate.relative_to(base).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return {"version": 1, "entries": entries, "total_bytes": sum(entry["bytes"] for entry in entries)}
