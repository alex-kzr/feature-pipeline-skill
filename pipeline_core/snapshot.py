"""An immutable identity for the worktree snapshot a task's verification commands run against.

Stage 8 runs a task's declared verification commands as runner-owned evidence
(:func:`pipeline_core.commands.run_verification_commands`). That evidence is only trustworthy
if it is bound to *which tree* the commands actually saw: a later, unrelated worktree edit — a
local documentation change, a half-finished experiment in another package — must not be able to
retroactively make a recorded pass read as though it described a different repository state.

:func:`capture_verification_snapshot` records that binding as a content-addressed
:class:`SnapshotIdentity`:

* every repository boundary's ``HEAD`` commit (the working root plus each initialised
  submodule), so the committed base is pinned;
* a single ``sha256`` over the *task-scoped* working-tree content — the bytes of every
  VCS-relevant path that matches the task's declared ``allowed_scope`` (all VCS-relevant paths
  when no scope is given). Paths under ``exclude_roots`` (the runner's own run directory) never
  enter the digest, so runner-owned churn cannot move the identity.

The identity is deterministic (same tree in, same ``token`` out) and fails closed: a working
root that is not inside a Git work tree, or any failing Git query, yields an *unavailable*
identity with a stated reason. :func:`require_verification_snapshot` turns that unavailable
identity into a raised :class:`SnapshotError` for callers that must not proceed without one.

This module creates no worktree, stages nothing, and writes nothing — "cleanup" is therefore
nothing to undo. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .worktree import (
    _git,
    _in_allowed_scope,
    _repository_boundaries,
    _vcs_relevant_paths,
    _within,
)

__all__ = [
    "SnapshotError",
    "SnapshotIdentity",
    "capture_verification_snapshot",
    "require_verification_snapshot",
]

_NO_REPOSITORY_BOUNDARY = (
    "no Git repository boundary at the working root; a verification snapshot identity "
    "cannot be recorded"
)


class SnapshotError(RuntimeError):
    """A verification snapshot identity that could not be established. ``code`` is stable."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SnapshotIdentity:
    """The immutable identity of the tree one verification pass runs against.

    ``token`` is ``"snapshot:<sha256>"`` over the boundary HEADs and the task-scoped content
    digest; it is empty when ``available`` is false. A frozen dataclass over tuples — nothing
    a later worktree edit can reach in place.
    """

    token: str
    boundaries: tuple[tuple[str, str], ...]
    scoped_digest: str
    available: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "token": self.token,
            "boundaries": [[name, commit] for name, commit in self.boundaries],
            "scoped_digest": self.scoped_digest,
            "available": self.available,
            "reason": self.reason,
        }


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def _unavailable(reason: str) -> SnapshotIdentity:
    return SnapshotIdentity("", (), "", available=False, reason=reason)


def capture_verification_snapshot(
    repo_root: str | Path,
    *,
    allowed_scope: Sequence[str] = (),
    exclude_roots: Sequence[str | Path] = (),
) -> SnapshotIdentity:
    """Record the immutable identity of the tree a verification pass will run against.

    ``allowed_scope`` is the task's repository-relative scope globs; when non-empty only the
    matching VCS-relevant paths contribute to the content digest, so an unrelated dirty file
    cannot move the identity. ``exclude_roots`` drops runner-owned paths (the run directory).
    A missing repository boundary or any failing Git query returns an unavailable identity.
    """
    root = Path(repo_root).resolve()
    excludes = [Path(entry).resolve() for entry in exclude_roots]
    boundaries = _repository_boundaries(root)
    if boundaries is None:
        return _unavailable(_NO_REPOSITORY_BOUNDARY)

    heads: list[tuple[str, str]] = []
    scoped: list[tuple[str, str]] = []
    for boundary in sorted(boundaries):
        name = boundary.relative_to(root).as_posix() or "."
        ok, out = _git(boundary, ["rev-parse", "HEAD"])
        if not ok or not out.strip():
            return _unavailable(
                f"git rev-parse HEAD failed under '{name}'; the verification snapshot "
                "identity is not trustworthy"
            )
        heads.append((name, out.strip()))

        relatives = _vcs_relevant_paths(boundary)
        if relatives is None:
            return _unavailable(
                f"git ls-files failed under '{name}'; the verification snapshot identity "
                "is not trustworthy"
            )
        for relative in sorted(relatives):
            absolute = Path((boundary / relative).resolve())
            if not _within(absolute, root):
                continue
            if any(_within(absolute, exclude) for exclude in excludes):
                continue
            key = absolute.relative_to(root).as_posix()
            if allowed_scope and not _in_allowed_scope(key, allowed_scope):
                continue
            if absolute.is_symlink():
                body = ("symlink -> " + os.readlink(absolute)).encode("utf-8")
            elif absolute.is_file():
                try:
                    body = absolute.read_bytes()
                except OSError:
                    return _unavailable(
                        f"'{key}' could not be read; the verification snapshot identity "
                        "is not trustworthy"
                    )
            else:
                body = b""
            scoped.append((key, "sha256:" + hashlib.sha256(body).hexdigest()))

    scoped_digest = _digest(sorted(scoped))
    token = "snapshot:" + _digest({"boundaries": heads, "scoped": scoped_digest})
    return SnapshotIdentity(token, tuple(heads), scoped_digest, available=True)


def require_verification_snapshot(
    repo_root: str | Path,
    *,
    allowed_scope: Sequence[str] = (),
    exclude_roots: Sequence[str | Path] = (),
) -> SnapshotIdentity:
    """:func:`capture_verification_snapshot`, raising :class:`SnapshotError` when it fails
    closed. Callers that must not verify against an unknown tree use this."""
    identity = capture_verification_snapshot(
        repo_root, allowed_scope=allowed_scope, exclude_roots=exclude_roots
    )
    if not identity.available:
        raise SnapshotError(
            identity.reason or "verification snapshot identity unavailable",
            "snapshot-unavailable",
        )
    return identity
