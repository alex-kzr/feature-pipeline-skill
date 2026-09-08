"""Deterministic Markdown projection of one task's durable lifecycle state.

**KLC-02** (``docs/plans/tasks/KLC-02_markdown-task-projection.md``).

``run.json`` (schema v3, see :mod:`feature_pipeline.infrastructure.state.schema_v3`) is the
sole authority for task lifecycle state. This module owns none of that policy — it only
renders an already-decided :class:`~feature_pipeline.domain.vocabulary.TaskStatus` value into
the human-facing view: the active board (``docs/kanban.md``) and the task file's
``## Status`` checkboxes and, for ``verified``, its ``## Result`` section.

The mapping (KLC-02 Requirements):

* ``pending`` / ``ready`` / ``blocked`` -> ``To Do`` (board card in ``## To Do``, task file
  ``To Do`` checked).
* ``running`` / ``implemented`` / ``verification_failed`` / ``repairing`` -> ``In Progress``.
* ``verified`` -> no board card, task file ``Done`` checked, and exactly one ``## Result``
  section upserted from the supplied :class:`CompletionEvidence`.

:func:`project_task_state` computes both the new board text and the new task-file text in
memory, validates both fully, and only then writes each with a replace-style
(temp-file-plus-``os.replace``) write. Missing and duplicate cards are recoverable divergence:
they converge to one canonical card. Repeated calls with the same inputs produce byte-identical
output.

Standard library only.
"""

from __future__ import annotations

import os
import posixpath
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "BoardProjectionError",
    "MalformedBoardError",
    "MalformedTaskError",
    "MissingCardError",
    "DuplicateCardError",
    "InvalidEvidenceError",
    "CommandEvidence",
    "CompletionEvidence",
    "project_task_state",
]


class BoardProjectionError(Exception):
    """Base error for :mod:`board_projection` — never raised directly."""


class MalformedBoardError(BoardProjectionError):
    """The board file does not have the expected ``## To Do`` / ``## In Progress`` shape."""


class MalformedTaskError(BoardProjectionError):
    """The task file does not have the expected ``## Status`` (or ``## Result``) shape."""


class MissingCardError(BoardProjectionError):
    """Compatibility type retained for callers of pre-convergent projection."""


class DuplicateCardError(BoardProjectionError):
    """Compatibility type retained for callers of pre-convergent projection."""


class InvalidEvidenceError(BoardProjectionError):
    """Completion evidence is missing, misplaced, or names a non-repository-relative path."""


# ==============================================================================================
# Evidence — the caller-supplied facts a ``verified`` projection renders. Only typed, closed
# fields are accepted; raw command output (which could carry secrets) is never one of them.
# ==============================================================================================


@dataclass(frozen=True)
class CommandEvidence:
    """One declared verification command's working directory, argv text, and exit code."""

    cwd: str
    command: str
    exit_code: int


@dataclass(frozen=True)
class CompletionEvidence:
    """Everything a ``verified`` ``## Result`` section renders."""

    completed_at: str
    run_id: str
    outcome: str
    repair_count: int
    gate_count: int
    task_verdict: str | None = None
    test_verdict: str | None = None
    commands: tuple[CommandEvidence, ...] = ()
    evidence_paths: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for path in self.evidence_paths:
            _require_repo_relative(path)


def _require_repo_relative(path: str) -> None:
    if not path:
        raise InvalidEvidenceError("evidence path must not be empty")
    if path.startswith("/") or path.startswith("\\"):
        raise InvalidEvidenceError(f"evidence path must be repository-relative: {path!r}")
    if "\\" in path:
        raise InvalidEvidenceError(f"evidence path must use forward slashes: {path!r}")
    # ``os.path.splitdrive`` only recognises a Windows drive on ``ntpath`` (i.e. on Windows);
    # on a POSIX runner it never reports one, so a Windows-style absolute path such as
    # ``"C:/Users/me/report.md"`` would otherwise slip through. Match a drive letter
    # explicitly so the rejection is identical on every OS (mirrors
    # ``pipeline_core.state``'s check).
    if re.match(r"^[A-Za-z]:[\\/]", path) or os.path.splitdrive(path)[0]:
        raise InvalidEvidenceError(f"evidence path must be repository-relative: {path!r}")


# ==============================================================================================
# State -> column mapping.
# ==============================================================================================

_TO_DO_STATES = frozenset({"pending", "ready", "blocked"})
_IN_PROGRESS_STATES = frozenset(
    {"running", "implemented", "verification_failed", "repairing"}
)
_VERIFIED_STATE = "verified"

_COLUMN_LABELS = {"to_do": "To Do", "in_progress": "In Progress", "none": "Done"}


def _target_column(state: str) -> str:
    if state in _TO_DO_STATES:
        return "to_do"
    if state in _IN_PROGRESS_STATES:
        return "in_progress"
    if state == _VERIFIED_STATE:
        return "none"
    raise BoardProjectionError(f"unsupported durable state: {state!r}")


# ==============================================================================================
# Public entry point.
# ==============================================================================================


def project_task_state(
    *,
    board_path: Path,
    task_path: Path,
    task_id: str,
    task_title: str,
    state: str,
    evidence: CompletionEvidence | None = None,
) -> None:
    """Project ``state`` for ``task_id``/``task_title`` onto ``board_path`` and ``task_path``.

    Both files are re-rendered and validated in memory before either replace-style write.
    """

    target = _target_column(state)
    if target == "none":
        if evidence is None:
            raise InvalidEvidenceError("the verified state requires completion evidence")
    elif evidence is not None:
        raise InvalidEvidenceError("completion evidence is only accepted for verified")

    board_display = _display_path(board_path)
    task_display = _display_path(task_path)
    try:
        board_text = _read_text_preserving_newlines(board_path)
    except OSError as exc:
        raise BoardProjectionError(f"read {board_display}: {exc}") from exc
    try:
        task_text = _read_text_preserving_newlines(task_path)
    except OSError as exc:
        raise BoardProjectionError(f"read {task_display}: {exc}") from exc

    link = _relative_link(board_path, task_path)
    try:
        new_board_text = _apply_board_transition(board_text, task_id, task_title, link, target)
    except BoardProjectionError as exc:
        raise exc.__class__(f"render {board_display}: {exc}") from exc
    try:
        new_task_text = _apply_task_transition(task_text, target, evidence)
    except BoardProjectionError as exc:
        raise exc.__class__(f"render {task_display}: {exc}") from exc

    _replace_write(board_path, new_board_text, "write-board", board_display)
    _replace_write(task_path, new_task_text, "write-task", task_display)


def _display_path(path: Path) -> str:
    """Return a repository-style path without exposing a host-specific absolute path."""

    parts = path.parts
    for marker in ("docs", "feature-pipeline-skill"):
        if marker in parts:
            return Path(*parts[parts.index(marker) :]).as_posix()
    return path.name


def _read_text_preserving_newlines(path: Path) -> str:
    """Read ``path`` as UTF-8 without universal-newline translation.

    ``Path.read_text`` translates every line ending to ``\\n`` before this module ever
    inspects it, which would make the CRLF/LF detection below always see LF. ``newline=""``
    disables that translation so the original bytes' line endings survive into the string.
    """

    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _relative_link(board_path: Path, task_path: Path) -> str:
    relative = os.path.relpath(task_path, start=board_path.parent)
    return Path(relative).as_posix()


def _replace_write(path: Path, text: str, operation: str, display_path: str) -> None:
    directory = path.parent
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=directory, prefix=f".{path.name}.", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except OSError as exc:
        if "tmp_name" in locals() and os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise BoardProjectionError(f"{operation} {display_path}: {exc}") from exc


# ==============================================================================================
# Board (``## To Do`` / ``## In Progress``) editing.
# ==============================================================================================


def _find_heading_once(lines: list[str], heading: str, *, after: int = 0) -> int:
    matches = [
        i for i, line in enumerate(lines) if i >= after and line.rstrip("\r\n") == heading
    ]
    if len(matches) != 1:
        raise MalformedBoardError(
            f"expected exactly one {heading!r} heading, found {len(matches)}"
        )
    return matches[0]


_CARD_RE = re.compile(r"^\s*- \[([^]]+)]\(([^)]+)\)")


def _normalise_link(link: str) -> str:
    return posixpath.normpath(link.replace("\\", "/"))


def _is_task_card(line: str, task_id: str, link: str) -> bool:
    match = _CARD_RE.match(line.rstrip("\r\n"))
    if match is None:
        return False
    label, card_link = match.groups()
    card_id = label.split(":", 1)[0].strip()
    return card_id == task_id or _normalise_link(card_link) == _normalise_link(link)


def _card_line(task_id: str, title: str, link: str, newline: str) -> str:
    return f"- [{task_id}: {title}]({link}){newline}"


def _insert_card(body: list[str], card_line: str, newline: str) -> list[str]:
    card_positions = [i for i, line in enumerate(body) if line.lstrip().startswith("- [")]
    if card_positions:
        insert_at = card_positions[-1] + 1
        return body[:insert_at] + [card_line] + body[insert_at:]
    if not body:
        return [newline, card_line]
    return body + [card_line]


def _upsert_column(
    body: list[str], task_id: str, link: str, card_line: str, newline: str
) -> list[str]:
    """Replace all matching cards by one canonical card, retaining surrounding Markdown."""

    rendered: list[str] = []
    inserted = False
    for line in body:
        if _is_task_card(line, task_id, link):
            if not inserted:
                rendered.append(card_line)
                inserted = True
            continue
        rendered.append(line)
    return rendered if inserted else _insert_card(rendered, card_line, newline)


def _remove_matching_cards(body: list[str], task_id: str, link: str) -> list[str]:
    return [line for line in body if not _is_task_card(line, task_id, link)]


def _apply_board_transition(
    text: str, task_id: str, title: str, link: str, target: str
) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)

    todo_idx = _find_heading_once(lines, "## To Do")
    in_progress_idx = _find_heading_once(lines, "## In Progress", after=todo_idx + 1)

    preamble = lines[: todo_idx + 1]
    todo_body = lines[todo_idx + 1 : in_progress_idx]
    in_progress_heading = lines[in_progress_idx : in_progress_idx + 1]
    next_heading = next(
        (i for i in range(in_progress_idx + 1, len(lines))
         if lines[i].rstrip("\r\n").startswith("## ")),
        len(lines),
    )
    in_progress_body = lines[in_progress_idx + 1 : next_heading]
    tail = lines[next_heading:]

    card_line = _card_line(task_id, title, link, newline)

    if target == "to_do":
        todo_body = _upsert_column(todo_body, task_id, link, card_line, newline)
        in_progress_body = _remove_matching_cards(in_progress_body, task_id, link)
    elif target == "in_progress":
        todo_body = _remove_matching_cards(todo_body, task_id, link)
        in_progress_body = _upsert_column(
            in_progress_body, task_id, link, card_line, newline
        )
    else:  # target == "none" (verified)
        todo_body = _remove_matching_cards(todo_body, task_id, link)
        in_progress_body = _remove_matching_cards(in_progress_body, task_id, link)

    return "".join(preamble + todo_body + in_progress_heading + in_progress_body + tail)


# ==============================================================================================
# Task file (``## Status`` / ``## Result``) editing.
# ==============================================================================================

_STATUS_LABELS = ("To Do", "In Progress", "Done")
_CHECKBOX_RE = re.compile(r"^- \[([ xX])\] (.+?)\s*$")


def _find_single_heading(lines: list[str], heading: str) -> int | None:
    matches = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == heading]
    if len(matches) > 1:
        raise MalformedTaskError(f"more than one {heading!r} heading")
    return matches[0] if matches else None


def _section_end(lines: list[str], start: int) -> int:
    for i in range(start + 1, len(lines)):
        if lines[i].rstrip("\r\n").startswith("## "):
            return i
    return len(lines)


def _parse_status_block(lines: list[str], start: int) -> int:
    """Return the index just past the three ``## Status`` checkbox lines."""

    if start + 3 > len(lines):
        raise MalformedTaskError("## Status block is truncated")
    labels: list[str] = []
    for offset in range(3):
        stripped = lines[start + offset].rstrip("\r\n")
        match = _CHECKBOX_RE.match(stripped)
        if match is None:
            raise MalformedTaskError(f"unexpected ## Status line: {stripped!r}")
        labels.append(match.group(2))
    if tuple(labels) != _STATUS_LABELS:
        raise MalformedTaskError(
            f"## Status checkboxes must read {_STATUS_LABELS}, got {tuple(labels)}"
        )
    return start + 3


def _render_status_block(lines: list[str], start: int, target_label: str) -> list[str]:
    rendered = []
    for offset, label in enumerate(_STATUS_LABELS):
        original = lines[start + offset]
        ending = original[len(original.rstrip("\r\n")) :]
        mark = "x" if label == target_label else " "
        rendered.append(f"- [{mark}] {label}{ending}")
    return rendered


def _render_result_section(evidence: CompletionEvidence, newline: str) -> list[str]:
    lines = [f"## Result{newline}", newline]
    lines.append(
        f"Completed {evidence.completed_at} for run `{evidence.run_id}` "
        f"— outcome: **{evidence.outcome}**.{newline}"
    )
    lines.append(newline)
    lines.append(f"- Repairs: {evidence.repair_count}{newline}")
    lines.append(f"- Gate failures: {evidence.gate_count}{newline}")
    lines.append(f"- Task verifier verdict: {evidence.task_verdict or 'none'}{newline}")
    lines.append(f"- Test verifier verdict: {evidence.test_verdict or 'none'}{newline}")
    if evidence.commands:
        lines.append(newline)
        lines.append(f"Verification commands:{newline}")
        for command in evidence.commands:
            lines.append(
                f"- `{command.cwd} -> {command.command}` — exit {command.exit_code}{newline}"
            )
    if evidence.evidence_paths:
        lines.append(newline)
        lines.append(f"Evidence:{newline}")
        for path in evidence.evidence_paths:
            lines.append(f"- {path}{newline}")
    lines.append(newline)
    return lines


def _apply_task_transition(
    text: str, target: str, evidence: CompletionEvidence | None
) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)

    status_idx = _find_single_heading(lines, "## Status")
    if status_idx is None:
        raise MalformedTaskError("missing ## Status heading")
    status_end = _parse_status_block(lines, status_idx + 1)
    new_status_lines = _render_status_block(lines, status_idx + 1, _COLUMN_LABELS[target])

    head = lines[: status_idx + 1] + new_status_lines

    if target != "none":
        return "".join(head + lines[status_end:])

    assert evidence is not None  # enforced by project_task_state
    result_block = _render_result_section(evidence, newline)
    result_indices = [
        index for index, line in enumerate(lines)
        if line.rstrip("\r\n") == "## Result"
    ]

    if not result_indices:
        gap = lines[status_end : status_end + 1]
        if gap and gap[0].strip("\r\n") == "":
            remainder = lines[status_end + 1 :]
        else:
            gap = [newline]
            remainder = lines[status_end:]
        return "".join(head + gap + result_block + remainder)

    if result_indices[0] < status_end:
        raise MalformedTaskError("## Result heading precedes the ## Status block")

    result_ranges = [(index, _section_end(lines, index)) for index in result_indices]
    rendered = head
    cursor = status_end
    for position, end in result_ranges:
        rendered.extend(lines[cursor:position])
        if position == result_indices[0]:
            rendered.extend(result_block)
        cursor = end
    rendered.extend(lines[cursor:])
    return "".join(rendered)
