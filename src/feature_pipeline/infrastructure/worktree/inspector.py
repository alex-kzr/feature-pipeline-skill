"""``GitWorktreeInspector`` — bounded, candidate-based executor-window attribution.

The window opens with :meth:`GitWorktreeInspector.capture_marker`: it walks every
repository boundary, records each boundary's index (path -> blob id, cheap — no file body),
the set of tracked paths, and the *bytes* of only the paths already dirty or untracked. It
closes with :meth:`GitWorktreeInspector.attribute`: it re-reads ``git status`` for the
candidate set (dirty or untracked at open, plus anything Git now reports changed), reads the
*after* bytes for those paths, resolves each *before* body from the recorded bytes or — for
a path that was clean at open — from its index blob, and subtracts the two.

Bytes read is therefore ``O(open dirty + open untracked + closed changed)``; the clean
majority of the repository is never opened. The subtraction, classification, and unified-diff
rendering are byte-for-byte the legacy full-snapshot inspector's, so a parity harness over
the characterization fixtures sees identical attribution (WT-01 AC-1).

Standard library only. This module never imports ``pipeline_core``.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from feature_pipeline.infrastructure.git.inspector import GitInspection
from feature_pipeline.infrastructure.git.inspector import inspect as _git_inspect
from feature_pipeline.infrastructure.git.safety import (
    GitPolicyError,
    parse_read_only_git,
)
from feature_pipeline.ports.worktree import (
    CONFIDENCE_KNOWN,
    CONFIDENCE_KNOWN_EMPTY,
    CONFIDENCE_UNAVAILABLE,
    IN_SCOPE,
    OUT_OF_SCOPE,
    BoundaryKind,
    CandidateChange,
    ChangeKind,
    RepositoryBoundary,
    WorktreeInspection,
    WorktreeInspectionError,
)

#: One read-only Git run in a directory: ``(repo_dir, argv) -> GitInspection``.
GitRunner = Callable[[str, Sequence[str]], GitInspection]


def _default_runner(repo_dir: str, argv: Sequence[str]) -> GitInspection:
    return _git_inspect(repo_dir, argv)


# --- captured bytes ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Body:
    """A file body captured (or found absent / unreadable) at one point in time."""

    data: bytes | None
    unreadable: bool = False
    is_symlink: bool = False

    @property
    def kind(self) -> str:
        if self.unreadable:
            return "unreadable"
        if self.data is None:
            return "absent"
        return "present"


@dataclass(frozen=True)
class _IndexEntry:
    blob: str
    boundary: str  # repo-relative POSIX path of the owning boundary ("" == working root)


@dataclass(frozen=True)
class WorktreeMarkerRecord:
    """The state one window opened in — produced by :meth:`GitWorktreeInspector.capture_marker`.

    Opaque to callers: it satisfies :class:`feature_pipeline.ports.worktree.WorktreeMarker`
    and is only ever handed back to :meth:`GitWorktreeInspector.attribute`.
    """

    repo_root: str
    available: bool
    reason: str | None
    boundaries: tuple[RepositoryBoundary, ...] = ()
    boundary_dirs: dict[str, str] = field(default_factory=dict)
    index: dict[str, _IndexEntry] = field(default_factory=dict)
    tracked: frozenset[str] = frozenset()
    open_bodies: dict[str, _Body] = field(default_factory=dict)
    exclude_roots: tuple[str, ...] = ()


# --- scope matching (verbatim from ``pipeline_core.worktree``) -----------------------------


def _scope_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 2] == "**":
                out.append(".*")
                index += 2
                if pattern[index : index + 1] == "/":
                    index += 1
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


def _in_allowed_scope(path: str, allowed_scope: Sequence[str]) -> bool:
    for raw in allowed_scope:
        pattern = Path(str(raw)).as_posix()
        if Path(pattern).is_absolute():
            raise WorktreeInspectionError(
                f"allowed scope entry '{raw}' is not repository-relative",
                "invalid-allowed-scope",
            )
        if path == pattern or _scope_regex(pattern).match(path):
            return True
    return False


# --- diff rendering (verbatim byte shape) -------------------------------------------------


def _unified_chunk(path: str, status: str, before: bytes, after: bytes) -> str:
    if b"\0" in before or b"\0" in after:
        return f"# {status} binary file: {path}\n"
    before_lines = before.decode("utf-8", errors="replace").splitlines(keepends=True)
    after_lines = after.decode("utf-8", errors="replace").splitlines(keepends=True)
    diff = difflib.unified_diff(
        before_lines, after_lines, fromfile=f"a/{path}", tofile=f"b/{path}"
    )
    return "".join(diff)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --- the inspector ------------------------------------------------------------------------


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _read_body(absolute: Path) -> _Body:
    """The working-tree body of one path *now*, bounded to this single file."""
    if absolute.is_symlink():
        try:
            target = os.readlink(absolute)
        except OSError:
            return _Body(None, unreadable=True, is_symlink=True)
        return _Body(("symlink -> " + target).encode("utf-8"), is_symlink=True)
    if not absolute.exists():
        return _Body(None)
    if not absolute.is_file():
        return _Body(None)
    try:
        return _Body(absolute.read_bytes())
    except OSError:
        return _Body(None, unreadable=True)


class GitWorktreeInspector:
    """Attributes one executor launch window at ``O(changed candidates)`` cost."""

    def __init__(self, *, runner: GitRunner | None = None) -> None:
        self._run: GitRunner = runner or _default_runner

    # -- boundary discovery ---------------------------------------------------------------

    def _is_work_tree(self, repo_dir: Path) -> bool:
        result = self._run(str(repo_dir), ["rev-parse", "--is-inside-work-tree"])
        return result.available and result.stdout.strip() == "true"

    def _walk_boundaries(
        self, root: Path
    ) -> tuple[list[tuple[RepositoryBoundary, Path]], None] | tuple[None, str]:
        if not self._is_work_tree(root):
            return None, (
                "no Git repository boundary at the working root; the executor window "
                "cannot be attributed"
            )
        found: list[tuple[RepositoryBoundary, Path]] = []
        seen: set[Path] = set()
        pending: list[tuple[Path, BoundaryKind]] = [(root, BoundaryKind.WORKING_ROOT)]
        while pending:
            repo, kind = pending.pop()
            repo = repo.resolve()
            if repo in seen:
                continue
            seen.add(repo)
            rel = "" if repo == root else repo.relative_to(root).as_posix()
            found.append((RepositoryBoundary(path=rel, kind=kind), repo))
            gitmodules = repo / ".gitmodules"
            text = gitmodules.read_text(encoding="utf-8") if gitmodules.is_file() else ""
            for raw in re.findall(r"(?m)^\s*path\s*=\s*(.+?)\s*$", text):
                candidate = (repo / raw.strip()).resolve()
                if not _within(candidate, root):
                    continue
                if self._is_work_tree(candidate):
                    child_kind = (
                        BoundaryKind.SUBMODULE
                        if gitmodules.is_file()
                        else BoundaryKind.NESTED_REPOSITORY
                    )
                    pending.append((candidate, child_kind))
        return found, None

    # -- status parsing -----------------------------------------------------------------

    def _status(
        self, repo_dir: Path
    ) -> tuple[set[str], set[str], None] | tuple[None, None, str]:
        """``(dirty, untracked, None)`` relative to ``repo_dir``, or ``(None, None, reason)``.

        ``dirty`` are tracked paths Git reports changed (a rename contributes both its old
        and new path); ``untracked`` are per-file, from ``ls-files --others`` — never a
        collapsed directory — so an excluded run directory can be filtered path by path.
        """
        status = self._run(str(repo_dir), ["status", "--porcelain", "-z"])
        if not status.available:
            return None, None, (
                f"git status failed under '{repo_dir.name}': "
                f"{status.reason or 'unknown error'}; the executor window cannot be attributed"
            )
        others = self._run(
            str(repo_dir), ["ls-files", "--others", "--exclude-standard", "-z"]
        )
        if not others.available:
            return None, None, (
                f"git ls-files --others failed under '{repo_dir.name}': "
                f"{others.reason or 'unknown error'}; the executor window cannot be attributed"
            )
        untracked = {chunk for chunk in others.stdout.split("\0") if chunk}
        dirty: set[str] = set()
        records = [chunk for chunk in status.stdout.split("\0") if chunk]
        skip_next = False
        for index, record in enumerate(records):
            if skip_next:
                skip_next = False
                continue
            if len(record) < 4:
                continue
            code, path = record[:2], record[3:]
            if code == "??" or code == "!!":
                continue  # untracked/ignored handled via ls-files --others
            if "R" in code or "C" in code:
                # ``XY new`` with ``old`` in the following NUL field: both are candidates.
                dirty.add(path)
                if index + 1 < len(records):
                    dirty.add(records[index + 1])
                    skip_next = True
                continue
            dirty.add(path)
        return dirty, untracked, None

    def _index(
        self, repo_dir: Path
    ) -> tuple[dict[str, str], None] | tuple[None, str]:
        result = self._run(str(repo_dir), ["ls-files", "-s", "-z"])
        if not result.available:
            return None, (
                f"git ls-files failed under '{repo_dir.name}': "
                f"{result.reason or 'unknown error'}; the executor window cannot be attributed"
            )
        entries: dict[str, str] = {}
        for record in result.stdout.split("\0"):
            if not record:
                continue
            # ``<mode> <object> <stage>\t<path>``
            meta, _, path = record.partition("\t")
            parts = meta.split()
            if len(parts) < 2 or not path:
                continue
            if parts[0] == "160000":
                continue  # a gitlink / submodule pointer, not a file body
            entries[path] = parts[1]
        return entries, None

    def _blob(self, repo_dir: Path, spec: str) -> _Body:
        """Raw bytes of one index/commit blob — no newline translation, bounded to one
        object."""
        try:
            parse_read_only_git(["show", spec])
        except GitPolicyError:
            return _Body(None, unreadable=True)
        try:
            completed = subprocess.run(
                ["git", "show", spec],
                cwd=repo_dir,
                capture_output=True,
                timeout=120.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _Body(None, unreadable=True)
        if completed.returncode != 0:
            return _Body(None, unreadable=True)
        return _Body(completed.stdout)

    # -- window open ------------------------------------------------------------------

    def capture_marker(
        self, repo_root: str, *, exclude_roots: Sequence[str] = ()
    ) -> WorktreeMarkerRecord:
        root = Path(repo_root).resolve()
        excludes = tuple(str(Path(entry).resolve()) for entry in exclude_roots)
        walked, reason = self._walk_boundaries(root)
        if walked is None:
            return WorktreeMarkerRecord(
                repo_root=str(root), available=False, reason=reason,
                exclude_roots=excludes,
            )

        index: dict[str, _IndexEntry] = {}
        tracked: set[str] = set()
        boundary_dirs: dict[str, str] = {}
        open_bodies: dict[str, _Body] = {}
        boundaries: list[RepositoryBoundary] = []

        for boundary, repo_dir in walked:
            boundary_dirs[boundary.path] = str(repo_dir)
            entries, err = self._index(repo_dir)
            if entries is None:
                return WorktreeMarkerRecord(
                    repo_root=str(root), available=False, reason=err,
                    exclude_roots=excludes,
                )
            for rel, blob in entries.items():
                key = self._key(root, repo_dir, rel)
                if key is None:
                    continue
                index[key] = _IndexEntry(blob=blob, boundary=boundary.path)
                tracked.add(key)

            dirty, untracked, status_err = self._status(repo_dir)
            if dirty is None or untracked is None:
                return WorktreeMarkerRecord(
                    repo_root=str(root), available=False, reason=status_err,
                    exclude_roots=excludes,
                )
            boundary_paths = {b.path for b, _ in walked if b.path}
            dirty_count = 0
            untracked_count = 0
            for rel in sorted(dirty | untracked):
                key = self._key(root, repo_dir, rel)
                if key is None or key in boundary_paths:
                    continue
                absolute = Path(os.path.abspath(repo_dir / rel))
                if any(_within(absolute, Path(exc)) for exc in excludes):
                    continue
                open_bodies[key] = _read_body(absolute)
                if rel in dirty:
                    dirty_count += 1
                else:
                    untracked_count += 1
            boundaries.append(
                RepositoryBoundary(
                    path=boundary.path,
                    kind=boundary.kind,
                    dirty_candidate_count=dirty_count,
                    untracked_candidate_count=untracked_count,
                )
            )

        return WorktreeMarkerRecord(
            repo_root=str(root),
            available=True,
            reason=None,
            boundaries=tuple(boundaries),
            boundary_dirs=boundary_dirs,
            index=index,
            tracked=frozenset(tracked),
            open_bodies=open_bodies,
            exclude_roots=excludes,
        )

    @staticmethod
    def _key(root: Path, repo_dir: Path, rel: str) -> str | None:
        absolute = Path(os.path.abspath(repo_dir / rel))
        if not _within(absolute, root):
            return None
        return absolute.relative_to(root).as_posix()

    # -- window close ---------------------------------------------------------------

    def attribute(
        self,
        marker: WorktreeMarkerRecord,
        repo_root: str,
        *,
        allowed_scope: Sequence[str],
        exclude_roots: Sequence[str] = (),
    ) -> WorktreeInspection:
        if not marker.available:
            reason = marker.reason or "worktree marker unavailable"
            return WorktreeInspection(
                available=False,
                confidence=CONFIDENCE_UNAVAILABLE,
                boundaries=marker.boundaries,
                candidates=(),
                diff_text=(
                    f"# attribution: {CONFIDENCE_UNAVAILABLE}\n# reason: {reason}\n"
                ),
                reason=reason,
            )

        root = Path(repo_root).resolve()
        excludes = tuple(
            str(Path(entry).resolve()) for entry in (exclude_roots or marker.exclude_roots)
        )

        boundary_paths = {b.path for b in marker.boundaries if b.path}

        # Candidate set: everything read at open, plus everything Git now reports changed.
        candidates: set[str] = set(marker.open_bodies)
        for boundary in marker.boundaries:
            repo_dir = Path(marker.boundary_dirs[boundary.path])
            dirty, untracked, status_err = self._status(repo_dir)
            if dirty is None or untracked is None:
                return WorktreeInspection(
                    available=False,
                    confidence=CONFIDENCE_UNAVAILABLE,
                    boundaries=marker.boundaries,
                    candidates=(),
                    diff_text=(
                        f"# attribution: {CONFIDENCE_UNAVAILABLE}\n"
                        f"# reason: {status_err}\n"
                    ),
                    reason=status_err,
                )
            for rel in dirty | untracked:
                key = self._key(root, repo_dir, rel)
                if key is None or key in boundary_paths:
                    continue
                absolute = Path(os.path.abspath(repo_dir / rel))
                if any(_within(absolute, Path(exc)) for exc in excludes):
                    continue
                candidates.add(key)

        changed: list[CandidateChange] = []
        chunks: list[str] = []
        ambiguous: str | None = None

        for key in sorted(candidates):
            before = self._before_body(marker, key)
            after = self._after_body(root, key)
            if (before.kind, _body_bytes(before)) == (after.kind, _body_bytes(after)):
                continue
            status = (
                ChangeKind.ADDED
                if before.kind == "absent"
                else ChangeKind.DELETED
                if after.kind == "absent"
                else ChangeKind.MODIFIED
            )
            digest = (
                _digest(after.data)
                if after.kind == "present" and after.data is not None
                else None
            )
            classification = (
                IN_SCOPE if _in_allowed_scope(key, allowed_scope) else OUT_OF_SCOPE
            )
            changed.append(
                CandidateChange(
                    path=key,
                    change=status,
                    classification=classification,
                    digest=digest,
                    is_symlink=after.is_symlink or before.is_symlink,
                    boundary=self._owning_boundary(marker, key),
                )
            )
            if "unreadable" in (before.kind, after.kind):
                ambiguous = ambiguous or key
                chunks.append(
                    f"# {status.value} file with unreadable content (attribution not "
                    f"trustworthy): {key}\n"
                )
                continue
            chunks.append(
                _unified_chunk(
                    key, status.value, before.data or b"", after.data or b""
                )
            )

        if ambiguous is not None:
            reason = (
                f"content of '{ambiguous}' could not be read; the executor window cannot "
                "be attributed with confidence"
            )
            header = f"# attribution: {CONFIDENCE_UNAVAILABLE}\n# reason: {reason}\n\n"
            return WorktreeInspection(
                available=False,
                confidence=CONFIDENCE_UNAVAILABLE,
                boundaries=marker.boundaries,
                candidates=tuple(changed),
                diff_text=header + "".join(chunks),
                reason=reason,
            )
        if not changed:
            return WorktreeInspection(
                available=True,
                confidence=CONFIDENCE_KNOWN_EMPTY,
                boundaries=marker.boundaries,
                candidates=(),
                diff_text=(
                    f"# attribution: {CONFIDENCE_KNOWN_EMPTY}\n"
                    "# no changes are attributable to the executor window\n"
                ),
            )
        return WorktreeInspection(
            available=True,
            confidence=CONFIDENCE_KNOWN,
            boundaries=marker.boundaries,
            candidates=tuple(changed),
            diff_text=f"# attribution: {CONFIDENCE_KNOWN}\n\n" + "".join(chunks),
        )

    # -- before/after body resolution --------------------------------------------------

    def _before_body(self, marker: WorktreeMarkerRecord, key: str) -> _Body:
        if key in marker.open_bodies:
            return marker.open_bodies[key]
        entry = marker.index.get(key)
        if entry is None:
            return _Body(None)  # untracked and unread at open -> genuinely new
        repo_dir = Path(marker.boundary_dirs.get(entry.boundary, marker.repo_root))
        rel = self._boundary_relative(marker, entry.boundary, key)
        return self._blob(repo_dir, f":{rel}")

    @staticmethod
    def _after_body(root: Path, key: str) -> _Body:
        return _read_body(root / key)

    @staticmethod
    def _boundary_relative(marker: WorktreeMarkerRecord, boundary: str, key: str) -> str:
        if not boundary:
            return key
        prefix = boundary + "/"
        return key[len(prefix) :] if key.startswith(prefix) else key

    @staticmethod
    def _owning_boundary(marker: WorktreeMarkerRecord, key: str) -> str:
        entry = marker.index.get(key)
        if entry is not None:
            return entry.boundary
        best = ""
        for boundary in marker.boundaries:
            if boundary.path and key.startswith(boundary.path + "/"):
                if len(boundary.path) > len(best):
                    best = boundary.path
        return best


def _body_bytes(body: _Body) -> bytes | None:
    return body.data if body.kind == "present" else None


__all__ = ["GitWorktreeInspector", "WorktreeMarkerRecord"]
