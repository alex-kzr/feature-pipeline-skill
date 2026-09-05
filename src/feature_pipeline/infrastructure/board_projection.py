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
(temp-file-plus-``os.replace``) write — a malformed board or task file, a missing or
duplicated active-board card, or invalid evidence raises *before* either file is touched, so
a failure never leaves a partially-updated file. Repeated calls with the same inputs produce
byte-identical output.

Standard library only.
"""

from __future__ import annotations

import os
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
    """A transition requires an existing active-board card and none was found."""


class DuplicateCardError(BoardProjectionError):
    """The same task's card appears more than once across the active board."""


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
    drive, _ = os.path.splitdrive(path)
    if drive:
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

    Both files are re-rendered in memory and validated before either is written, so a
    :class:`BoardProjectionError` never leaves a partial update on disk.
    """

    target = _target_column(state)
    if target == "none":
        if evidence is None:
            raise InvalidEvidenceError("the verified state requires completion evidence")
    elif evidence is not None:
        raise InvalidEvidenceError("completion evidence is only accepted for verified")

    board_text = _read_text_preserving_newlines(board_path)
    task_text = _read_text_preserving_newlines(task_path)

    link = _relative_link(board_path, task_path)
    new_board_text = _apply_board_transition(board_text, task_id, task_title, link, target)
    new_task_text = _apply_task_transition(task_text, target, evidence)

    _replace_write(board_path, new_board_text)
    _replace_write(task_path, new_task_text)


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


def _replace_write(path: Path, text: str) -> None:
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


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


def _find_card_positions(body: list[str], link: str) -> list[int]:
    needle = f"]({link})"
    return [i for i, line in enumerate(body) if needle in line]


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


def _remove_card(body: list[str], index: int) -> list[str]:
    remaining = body[:index] + body[index + 1 :]
    if not any(line.lstrip().startswith("- [") for line in remaining):
        return []
    return remaining


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
    tail_body = lines[in_progress_idx + 1 :]

    todo_hits = _find_card_positions(todo_body, link)
    in_progress_hits = _find_card_positions(tail_body, link)
    if len(todo_hits) + len(in_progress_hits) > 1:
        raise DuplicateCardError(
            f"card for {task_id!r} ({link}) appears more than once on the active board"
        )

    card_line = _card_line(task_id, title, link, newline)

    if target == "to_do":
        if todo_hits:
            todo_body = todo_body[: todo_hits[0]] + [card_line] + todo_body[todo_hits[0] + 1 :]
        elif in_progress_hits:
            tail_body = _remove_card(tail_body, in_progress_hits[0])
            todo_body = _insert_card(todo_body, card_line, newline)
        else:
            raise MissingCardError(
                f"no active-board card for {task_id!r} ({link}) to move to To Do"
            )
    elif target == "in_progress":
        if in_progress_hits:
            tail_body = (
                tail_body[: in_progress_hits[0]]
                + [card_line]
                + tail_body[in_progress_hits[0] + 1 :]
            )
        elif todo_hits:
            todo_body = _remove_card(todo_body, todo_hits[0])
            tail_body = _insert_card(tail_body, card_line, newline)
        else:
            raise MissingCardError(
                f"no active-board card for {task_id!r} ({link}) to move to In Progress"
            )
    else:  # target == "none" (verified)
        if todo_hits:
            todo_body = _remove_card(todo_body, todo_hits[0])
        elif in_progress_hits:
            tail_body = _remove_card(tail_body, in_progress_hits[0])
        # Already absent from both columns: idempotent no-op.

    return "".join(preamble + todo_body + in_progress_heading + tail_body)


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
    result_idx = _find_single_heading(lines, "## Result")
    result_block = _render_result_section(evidence, newline)

    if result_idx is None:
        gap = lines[status_end : status_end + 1]
        if gap and gap[0].strip("\r\n") == "":
            remainder = lines[status_end + 1 :]
        else:
            gap = [newline]
            remainder = lines[status_end:]
        return "".join(head + gap + result_block + remainder)

    if result_idx < status_end:
        raise MalformedTaskError("## Result heading precedes the ## Status block")
    result_end = _section_end(lines, result_idx)
    between = lines[status_end:result_idx]
    return "".join(head + between + result_block + lines[result_end:])
