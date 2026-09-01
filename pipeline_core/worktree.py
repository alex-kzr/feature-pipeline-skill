"""Runner-owned attribution of the implementation diff to one executor launch window.

The runner cannot trust an executor's prose about what it changed, and it must not let
pre-existing workspace dirt or its own run/lock/report artifacts leak into the record of
one generation's work. This module produces that evidence independently:

* :func:`capture_snapshot` takes a content snapshot of every VCS-relevant path across the
  repository boundary — the working root plus any declared nested repository — immediately
  before and after a single executor launch, skipping the runner-owned run directory.
* :func:`attribute_window` subtracts the before-snapshot from the after-snapshot. A path
  that only carried a pre-existing change has identical content on both sides and drops
  out; a path the executor further modified survives because its bytes changed. What
  remains is classified against the task's declared allowed scope.
* :func:`persist_attribution` writes ``implementation-manifest-<n>.json`` (every attributed
  path with its change status, content digest, and ``in_allowed_scope`` / ``out_of_scope``
  classification) and ``implementation-diff-<n>.md`` (the unified diff for exactly that
  window with an explicit attribution state), never overwriting an existing generation.

A trustworthy empty window is :data:`STATE_KNOWN_EMPTY`. A window whose attribution
cannot be trusted — no repository boundary, a failed Git query, a changed file whose
content could not be read — is :data:`STATE_UNAVAILABLE` with a stated reason, and is
never collapsed to an empty delta.

Every Git call goes through :class:`~pipeline_core.git_port.GitPort` as literal argv; this
module adds no staging, commit, or release behaviour.

Standard library only.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .artifacts import write_json_atomic, write_text_atomic
from .git_port import GitPort, GitSafetyError
from .reports import LaunchArtifacts
from .state import repo_relative

#: The three distinct persisted attribution states (AC-4).
STATE_KNOWN = "known"
STATE_KNOWN_EMPTY = "known-empty"
STATE_UNAVAILABLE = "unavailable"

IN_SCOPE = "in_allowed_scope"
OUT_OF_SCOPE = "out_of_scope"


class WorktreeError(RuntimeError):
    """A worktree-attribution failure. ``code`` is a stable, machine-readable reason."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


# --- snapshot model --------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotFile:
    """One VCS-relevant path captured at a point in time.

    ``data`` holds the file bytes. ``data is None`` with ``unreadable=False`` means the
    path is tracked but currently absent from the working tree (a deletion); ``data is
    None`` with ``unreadable=True`` means the file exists but its content could not be
    read, which makes any delta touching it ambiguous.
    """

    data: bytes | None
    unreadable: bool = False


@dataclass(frozen=True)
class WorktreeSnapshot:
    """A content snapshot of one repository boundary set, or an explained failure."""

    files: dict[str, SnapshotFile]
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class ChangedPath:
    """One path attributed to the executor window."""

    path: str
    status: str  # 'added' | 'modified' | 'deleted'
    digest: str | None  # 'sha256:<hex>' of the after-content; None for a deletion
    classification: str  # IN_SCOPE | OUT_OF_SCOPE


@dataclass(frozen=True)
class WindowAttribution:
    """The computed delta for one executor window before it is persisted."""

    state: str
    changed_files: tuple[ChangedPath, ...]
    diff_text: str
    reason: str | None = None


@dataclass(frozen=True)
class AttributionResult:
    """The persisted outcome: state, evidence rows, and repo-relative artifact refs."""

    state: str
    reason: str | None
    changed_files: list[dict[str, object]]
    manifest: str
    diff: str


# --- Git plumbing ---------------------------------------------------------------------------


def _git(repo: Path, argv: list[str]) -> tuple[bool, str]:
    """Run one read-only Git argv in ``repo``; ``(ok, stdout)`` with ``ok`` false on any
    non-zero exit or a policy rejection."""
    try:
        result = GitPort(repo).run(argv)
    except GitSafetyError:
        return False, ""
    if result.returncode != 0:
        return False, ""
    return True, result.stdout


def _repository_boundaries(root: Path) -> list[Path] | None:
    """The working root plus every declared, initialised nested repository under it.

    ``None`` when the working root is not inside a Git work tree at all — the caller turns
    that into an ``unavailable`` attribution rather than an empty one.
    """
    if not _git(root, ["rev-parse", "--is-inside-work-tree"])[0]:
        return None
    boundaries: list[Path] = []
    seen: set[Path] = set()
    pending = [root]
    while pending:
        repo = pending.pop().resolve()
        if repo in seen:
            continue
        seen.add(repo)
        boundaries.append(repo)
        gitmodules = repo / ".gitmodules"
        text = gitmodules.read_text(encoding="utf-8") if gitmodules.is_file() else ""
        for raw in re.findall(r"(?m)^\s*path\s*=\s*(.+?)\s*$", text):
            candidate = (repo / raw.strip()).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if _git(candidate, ["rev-parse", "--is-inside-work-tree"])[0]:
                pending.append(candidate)
    return boundaries


def _vcs_relevant_paths(repo: Path) -> set[str] | None:
    """Tracked plus non-ignored untracked paths for one boundary, or ``None`` on failure."""
    found: set[str] = set()
    for argv in (["ls-files", "-z"], ["ls-files", "--others", "--exclude-standard", "-z"]):
        ok, out = _git(repo, argv)
        if not ok:
            return None
        found.update(part for part in out.split("\0") if part)
    return found


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def capture_snapshot(
    repo_root: str | Path, *, exclude_roots: Sequence[str | Path] = ()
) -> WorktreeSnapshot:
    """Capture VCS-relevant file content across every repository boundary under ``repo_root``.

    Paths under any ``exclude_roots`` entry (the runner's own run/lock/report directory) are
    dropped so runner-owned churn never enters an executor window. A missing repository
    boundary or a failed ``git ls-files`` yields an unavailable snapshot with a reason.
    """
    root = Path(repo_root).resolve()
    excludes = [Path(entry).resolve() for entry in exclude_roots]
    boundaries = _repository_boundaries(root)
    if boundaries is None:
        return WorktreeSnapshot(
            {}, available=False,
            reason=(
                "no Git repository boundary at the working root; the executor window "
                "cannot be attributed"
            ),
        )
    files: dict[str, SnapshotFile] = {}
    for boundary in boundaries:
        relatives = _vcs_relevant_paths(boundary)
        if relatives is None:
            return WorktreeSnapshot(
                {}, available=False,
                reason=(
                    f"git ls-files failed under '{_display(boundary, root)}'; the executor "
                    "window cannot be attributed"
                ),
            )
        for relative in relatives:
            absolute = (boundary / relative).resolve()
            if not _within(absolute, root):
                continue
            if any(_within(absolute, exclude) for exclude in excludes):
                continue
            key = absolute.relative_to(root).as_posix()
            if not absolute.is_file():
                files[key] = SnapshotFile(None)
                continue
            try:
                files[key] = SnapshotFile(absolute.read_bytes())
            except OSError:
                files[key] = SnapshotFile(None, unreadable=True)
    return WorktreeSnapshot(files, available=True)


def _display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:
        return path.name


# --- scope matching -----------------------------------------------------------------------


def _scope_regex(pattern: str) -> re.Pattern[str]:
    """Translate a repository-relative scope glob (``*`` within a segment, ``**`` across
    segments) to an anchored regex. No other metacharacter is honoured."""
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
            raise WorktreeError(
                f"allowed scope entry '{raw}' is not repository-relative",
                "invalid-allowed-scope",
            )
        if path == pattern or _scope_regex(pattern).match(path):
            return True
    return False


# --- window attribution -----------------------------------------------------------------------


def _file_state(entry: SnapshotFile | None) -> tuple[str, bytes | None]:
    if entry is None or (entry.data is None and not entry.unreadable):
        return ("absent", None)
    if entry.unreadable:
        return ("unreadable", None)
    return ("present", entry.data)


def _unified_chunk(path: str, status: str, before: bytes, after: bytes) -> str:
    if b"\0" in before or b"\0" in after:
        return f"# {status} binary file: {path}\n"
    before_lines = before.decode("utf-8", errors="replace").splitlines(keepends=True)
    after_lines = after.decode("utf-8", errors="replace").splitlines(keepends=True)
    diff = difflib.unified_diff(
        before_lines, after_lines, fromfile=f"a/{path}", tofile=f"b/{path}"
    )
    return "".join(diff)


def attribute_window(
    before: WorktreeSnapshot,
    after: WorktreeSnapshot,
    *,
    allowed_scope: Sequence[str],
) -> WindowAttribution:
    """Subtract ``before`` from ``after`` and classify what the executor actually changed."""
    for snapshot in (before, after):
        if not snapshot.available:
            reason = snapshot.reason or "worktree snapshot unavailable"
            return WindowAttribution(
                STATE_UNAVAILABLE, (),
                f"# attribution: {STATE_UNAVAILABLE}\n# reason: {reason}\n",
                reason=reason,
            )

    changed: list[ChangedPath] = []
    chunks: list[str] = []
    ambiguous: str | None = None
    for key in sorted(set(before.files) | set(after.files)):
        before_state = _file_state(before.files.get(key))
        after_state = _file_state(after.files.get(key))
        if before_state == after_state:
            continue
        status = (
            "added" if before_state[0] == "absent"
            else "deleted" if after_state[0] == "absent"
            else "modified"
        )
        digest = (
            "sha256:" + hashlib.sha256(after_state[1]).hexdigest()
            if after_state[0] == "present"
            else None
        )
        classification = IN_SCOPE if _in_allowed_scope(key, allowed_scope) else OUT_OF_SCOPE
        changed.append(ChangedPath(key, status, digest, classification))
        if "unreadable" in (before_state[0], after_state[0]):
            ambiguous = ambiguous or key
            chunks.append(
                f"# {status} file with unreadable content (attribution not trustworthy): "
                f"{key}\n"
            )
            continue
        chunks.append(
            _unified_chunk(key, status, before_state[1] or b"", after_state[1] or b"")
        )

    if ambiguous is not None:
        reason = (
            f"content of '{ambiguous}' could not be read; the executor window cannot be "
            "attributed with confidence"
        )
        header = f"# attribution: {STATE_UNAVAILABLE}\n# reason: {reason}\n\n"
        return WindowAttribution(
            STATE_UNAVAILABLE, tuple(changed), header + "".join(chunks), reason=reason
        )
    if not changed:
        return WindowAttribution(
            STATE_KNOWN_EMPTY, (),
            f"# attribution: {STATE_KNOWN_EMPTY}\n"
            "# no changes are attributable to the executor window\n",
        )
    return WindowAttribution(
        STATE_KNOWN, tuple(changed), f"# attribution: {STATE_KNOWN}\n\n" + "".join(chunks)
    )


# --- persistence -----------------------------------------------------------------------------


def _rows(changed: Sequence[ChangedPath]) -> list[dict[str, object]]:
    return [
        {
            "path": entry.path,
            "status": entry.status,
            "digest": entry.digest,
            "classification": entry.classification,
        }
        for entry in changed
    ]


def persist_attribution(
    artifacts: LaunchArtifacts,
    attribution: WindowAttribution,
    *,
    task_id: str,
    attempt: int,
    generation: int,
    executor_report: str | Path,
    repo_root: str | Path,
) -> AttributionResult:
    """Write the manifest and diff for one generation, never overwriting an existing one."""
    manifest_path = Path(artifacts.implementation_manifest)
    diff_path = Path(artifacts.implementation_diff)
    if manifest_path.exists() or diff_path.exists():
        raise WorktreeError(
            f"{task_id}: implementation evidence for launch {generation} already exists",
            "implementation-evidence-overwrite",
        )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    rows = _rows(attribution.changed_files)
    write_json_atomic(
        manifest_path,
        {
            "task_id": task_id,
            "attempt": attempt,
            "generation": generation,
            "executor_report": repo_relative(executor_report, repo_root),
            "attribution_state": attribution.state,
            "reason": attribution.reason,
            "changed_files": rows,
        },
        repo_root=repo_root,
    )
    write_text_atomic(
        diff_path,
        attribution.diff_text or f"# attribution: {attribution.state}\n",
        repo_root=repo_root,
    )
    return AttributionResult(
        state=attribution.state,
        reason=attribution.reason,
        changed_files=rows,
        manifest=repo_relative(manifest_path, repo_root),
        diff=repo_relative(diff_path, repo_root),
    )


def attribute_executor_window(
    before: WorktreeSnapshot,
    repo_root: str | Path,
    *,
    artifacts: LaunchArtifacts,
    task_id: str,
    allowed_scope: Sequence[str],
    attempt: int,
    generation: int,
    executor_report: str | Path,
    exclude_roots: Sequence[str | Path] = (),
) -> AttributionResult:
    """Close one executor window: capture the after-snapshot, attribute the delta against
    ``allowed_scope``, and persist the manifest and diff. Returns the persisted result."""
    after = capture_snapshot(repo_root, exclude_roots=exclude_roots)
    attribution = attribute_window(before, after, allowed_scope=allowed_scope)
    return persist_attribution(
        artifacts,
        attribution,
        task_id=task_id,
        attempt=attempt,
        generation=generation,
        executor_report=executor_report,
        repo_root=repo_root,
    )
