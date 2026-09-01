"""Native Markdown task-file reader for the portable core.

A task file's ``## Execution Metadata`` and ``## Acceptance Criteria`` blocks are parsed into
one validated :class:`~schemas.contracts.TaskSpec` — the same normalized model a JSON plan
entry produces through :func:`pipeline_core.legacy_adapter.adapt_task_spec`. Historical task
files that predate the metadata block stay executable: their ``## Definition of Done`` becomes
synthetic ``AC-1 … AC-n`` and every other field comes from caller-supplied :class:`TaskDefaults`.
The source file is only ever read — never rewritten to look uniform.

Validation is fail-closed and happens here, before any run artifact or executor process
exists: an unknown field, an absent required field, an unsafe path, a shell operator in a
command, or non-sequential acceptance-criteria IDs all raise :class:`TaskFileError`.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from schemas.contracts import (
    AcceptanceCriterionSpec,
    CommandSpec,
    SchemaError,
    TaskSpec,
    validate_acceptance_criteria,
)

__all__ = [
    "TaskDefaults",
    "TaskFileError",
    "load_task_spec",
    "parse_execution_metadata",
    "render_blocker_entry",
    "synthesize_acceptance_criteria",
    "upsert_blockers_section",
]

BLOCKERS_HEADING = "## Blockers"
_BLOCKER_KEY_RE = re.compile(r"^-\s+\[(?P<key>[^\]]+)\]")


class TaskFileError(SchemaError):
    """The task file does not meet the execution task contract."""


@dataclass(frozen=True)
class TaskDefaults:
    """Project-supplied values for a task file that carries no ``## Execution Metadata`` block.

    Nothing here is inferred by the core; a caller (a project profile or extension) decides
    what a historical file's type, executor, and checks are.
    """

    task_type: str
    executor: str
    max_repair_attempts: int = 2
    out_of_scope: tuple[str, ...] = ()
    required_skills: tuple[str, ...] = ()
    documentation_impact: tuple[str, ...] = ()
    verification_commands: tuple[CommandSpec, ...] = ()


_HEADING_RE = re.compile(r"^#\s+(?P<id>[A-Za-z]{2,6}-\d{1,4})\s*[-—–]\s*(?P<title>.+?)\s*$")
_FILENAME_ID_RE = re.compile(r"^(?P<id>[A-Za-z]{2,6}-\d{1,4})_.+\.md$")
_META_FIELD_RE = re.compile(r"^-\s+(?P<key>[A-Za-z][A-Za-z ]*?)\s*:\s*(?P<value>.*)$")
_COMMAND_RE = re.compile(
    r"^\s+-\s+`?(?P<cwd>[^`]+?)`?\s*(?:->|→)\s*`?(?P<command>.+?)`?\s*$"
)
_AC_RE = re.compile(r"^-\s+\[(?P<done>[ xX])\]\s+(?P<id>AC-\d+)\s*[-—–]\s*(?P<text>.+?)\s*$")
_CHECKBOX_RE = re.compile(r"^-\s+\[(?P<done>[ xX])\]\s+(?P<text>.+?)\s*$")
_BACKTICK_TOKEN_RE = re.compile(r"`([^`]+)`")

#: Markdown field name (lower-cased, whitespace-collapsed) -> TaskSpec keyword.
_FIELD_KEYS = {
    "type": "task_type",
    "executor": "executor",
    "depends on": "depends_on",
    "allowed scope": "allowed_scope",
    "out of scope": "out_of_scope",
    "required skills": "required_skills",
    "maximum repair attempts": "max_repair_attempts",
    "documentation impact": "documentation_impact",
    "verification commands": "verification_commands",
    "blocking conditions": "blocking_conditions",
    "verification tier": "verification_tier",
    "accepts scoped": "accepts_scoped",
    "deferred verification commands": "deferred_verification_commands",
}

_REQUIRED_DECLARED = (
    "task_type", "executor", "depends_on", "allowed_scope", "out_of_scope",
    "required_skills", "documentation_impact", "verification_commands",
)

_LIST_KEYS = ("depends_on", "allowed_scope", "out_of_scope", "required_skills",
              "documentation_impact", "accepts_scoped")

_SUBLIST_KEYS = ("verification_commands", "deferred_verification_commands")


def _is_none(value: str) -> bool:
    return value.strip().lower() == "none"


def _strip_backticks(value: str) -> str:
    token = value.strip()
    if len(token) > 1 and token.startswith("`") and token.endswith("`"):
        token = token[1:-1]
    return token.strip()


def _split_list(value: str) -> list[str]:
    if _is_none(value):
        return []
    return [_strip_backticks(part) for part in value.split(",") if part.strip()]


def _section(lines: Sequence[str], heading: str) -> list[tuple[int, str]]:
    """The numbered lines under one ``## Heading``, up to the next ``## `` heading."""
    out: list[tuple[int, str]] = []
    inside = False
    for index, line in enumerate(lines, start=1):
        if line.startswith("## "):
            inside = line.strip().lower() == heading.strip().lower()
            continue
        if inside:
            out.append((index, line))
    return out


def _has_section(lines: Sequence[str], heading: str) -> bool:
    return any(line.startswith("## ") and line.strip().lower() == heading.strip().lower()
              for line in lines)


def parse_execution_metadata(text: str) -> dict[str, object] | None:
    """The raw ``## Execution Metadata`` fields, or ``None`` when the block is absent.

    Values are returned as written (strings, and a list of ``(cwd, command)`` pairs for the
    two sub-list fields); semantic validation happens in :func:`load_task_spec`.
    """
    lines = text.splitlines()
    if not _has_section(lines, "## Execution Metadata"):
        return None
    section = _section(lines, "## Execution Metadata")

    values: dict[str, object] = {}
    seen: dict[str, int] = {}
    commands: list[tuple[str, str]] = []
    deferred: list[tuple[str, str]] = []
    current: str | None = None

    for line_no, raw_line in section:
        if not raw_line.strip():
            continue
        if raw_line.startswith((" ", "\t")) and current in _SUBLIST_KEYS:
            match = _COMMAND_RE.match(raw_line)
            if not match:
                raise TaskFileError(
                    f"line {line_no}: unparseable verification command '{raw_line.strip()}' "
                    f"— expected '`<cwd>` -> `<command>`'"
                )
            target = commands if current == "verification_commands" else deferred
            target.append((match.group("cwd").strip(), match.group("command").strip()))
            continue

        field_match = _META_FIELD_RE.match(raw_line)
        if not field_match:
            continue
        name = " ".join(field_match.group("key").strip().lower().split())
        key = _FIELD_KEYS.get(name)
        if key is None:
            raise TaskFileError(
                f"line {line_no}: unknown metadata field '{field_match.group('key').strip()}'"
            )
        if key in seen:
            raise TaskFileError(
                f"line {line_no}: duplicate field '{field_match.group('key').strip()}' "
                f"(first at line {seen[key]})"
            )
        seen[key] = line_no
        current = key
        value = field_match.group("value").strip()
        if key in _SUBLIST_KEYS:
            if value and not _is_none(value):
                raise TaskFileError(
                    f"line {line_no}: '{field_match.group('key').strip()}' takes an indented "
                    f"sub-list, not an inline value"
                )
            values[key] = "none" if _is_none(value) else ""
            continue
        if not value:
            raise TaskFileError(
                f"line {line_no}: field '{field_match.group('key').strip()}' is empty — write "
                f"'none' to state that explicitly"
            )
        values[key] = value

    values["_commands"] = commands
    values["_deferred"] = deferred
    return values


def synthesize_acceptance_criteria(
    items: Sequence[object],
) -> tuple[AcceptanceCriterionSpec, ...]:
    """Assign synthetic ``AC-1 … AC-n`` IDs to ordered ``Definition of Done`` entries."""
    try:
        return validate_acceptance_criteria(items)
    except SchemaError as exc:
        raise TaskFileError(str(exc)) from None


def _acceptance_criteria(lines: Sequence[str]) -> tuple[tuple[AcceptanceCriterionSpec, ...], bool]:
    """``(criteria, synthesized)`` — parsed ``## Acceptance Criteria`` or synthesized DoD."""
    if _has_section(lines, "## Acceptance Criteria"):
        parsed: list[dict[str, object]] = []
        for _line_no, line in _section(lines, "## Acceptance Criteria"):
            match = _AC_RE.match(line.strip())
            if match:
                parsed.append({
                    "id": match.group("id"),
                    "text": match.group("text").strip(),
                    "checked": match.group("done").lower() == "x",
                })
        if not parsed:
            raise TaskFileError("'## Acceptance Criteria' is present but lists no criteria")
        try:
            return validate_acceptance_criteria(parsed), False
        except SchemaError as exc:
            raise TaskFileError(str(exc)) from None

    dod: list[str] = []
    for _line_no, line in _section(lines, "## Definition of Done"):
        match = _CHECKBOX_RE.match(line.strip())
        if match:
            dod.append(match.group("text").strip())
    if dod:
        return synthesize_acceptance_criteria(dod), True
    return (), False


def _affected_paths(lines: Sequence[str]) -> list[str]:
    paths: list[str] = []
    for _line_no, line in _section(lines, "## Affected Files / Components"):
        for token in _BACKTICK_TOKEN_RE.findall(line):
            candidate = token.strip().replace("\\", "/")
            if candidate and not candidate.startswith(("~", "/")):
                paths.append(candidate)
    return paths


def _resolve_identity(lines: Sequence[str], path: Path) -> tuple[str, str]:
    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            return match.group("id"), match.group("title").strip()
    filename_match = _FILENAME_ID_RE.match(path.name)
    if filename_match:
        return filename_match.group("id"), ""
    raise TaskFileError(
        f"{path.name}: no '# <TASK-ID> - <title>' heading and the filename does not carry an ID"
    )


def load_task_spec(path: Path, *, defaults: TaskDefaults | None = None) -> TaskSpec:
    """Normalize one Markdown task file into a validated :class:`TaskSpec`.

    A file with an ``## Execution Metadata`` block is normalized from that block alone;
    ``defaults`` is only consulted for a historical, block-less file. When such a file is given
    and ``defaults`` is ``None`` the call fails closed — real execution never runs a task whose
    metadata is neither declared nor supplied.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    task_id, title = _resolve_identity(lines, path)
    rel = path.as_posix()

    meta = parse_execution_metadata(text)
    criteria, synthesized = _acceptance_criteria(lines)

    if meta is None:
        if defaults is None:
            raise TaskFileError(
                f"{rel}: no '## Execution Metadata' block and no defaults supplied — a "
                f"historical task needs explicit caller defaults to execute"
            )
        return _spec_from_defaults(task_id, title, rel, lines, defaults, criteria, synthesized)

    return _spec_from_declared(task_id, title, rel, meta, criteria)


def _spec_from_declared(
    task_id: str,
    title: str,
    rel: str,
    meta: Mapping[str, object],
    criteria: tuple[AcceptanceCriterionSpec, ...],
) -> TaskSpec:
    for key in _REQUIRED_DECLARED:
        if key not in meta:
            raise TaskFileError(
                f"{rel}: declared '## Execution Metadata' is missing required field "
                f"'{key.replace('_', ' ')}'"
            )

    kwargs: dict[str, object] = {
        "id": task_id,
        "title": title,
        "path": rel,
        "task_type": str(meta["task_type"]),
        "executor": str(meta["executor"]),
        "acceptance_criteria": criteria,
        "metadata_source": "declared",
        "defaults_applied": (),
    }

    for key in _LIST_KEYS:
        if key in meta:
            kwargs[key] = tuple(_split_list(str(meta[key])))

    if "max_repair_attempts" in meta:
        token = _strip_backticks(str(meta["max_repair_attempts"]))
        if not token.isdigit():
            raise TaskFileError(
                f"{rel}: 'Maximum repair attempts' must be a non-negative integer, got "
                f"'{meta['max_repair_attempts']}'"
            )
        kwargs["max_repair_attempts"] = int(token)

    if "verification_tier" in meta:
        kwargs["verification_tier"] = _strip_backticks(str(meta["verification_tier"])).lower()

    if "blocking_conditions" in meta:
        raw = str(meta["blocking_conditions"])
        kwargs["blocking_conditions"] = None if _is_none(raw) else raw

    kwargs["verification_commands"] = tuple(
        {"cwd": cwd, "command": command} for cwd, command in meta.get("_commands", ())  # type: ignore[union-attr]
    )
    kwargs["deferred_verification_commands"] = tuple(
        {"cwd": cwd, "command": command} for cwd, command in meta.get("_deferred", ())  # type: ignore[union-attr]
    )

    try:
        return TaskSpec.build(**kwargs)  # type: ignore[arg-type]
    except SchemaError as exc:
        raise TaskFileError(f"{rel}: {exc}") from None


def _spec_from_defaults(
    task_id: str,
    title: str,
    rel: str,
    lines: Sequence[str],
    defaults: TaskDefaults,
    criteria: tuple[AcceptanceCriterionSpec, ...],
    synthesized: bool,
) -> TaskSpec:
    allowed_scope = _affected_paths(lines)
    if not allowed_scope:
        raise TaskFileError(
            f"{rel}: no '## Affected Files / Components' paths to use as the allowed scope"
        )

    applied = ["task_type", "executor", "out_of_scope", "required_skills",
               "documentation_impact", "verification_commands", "max_repair_attempts"]
    if synthesized:
        applied.append("acceptance_criteria")

    try:
        return TaskSpec.build(
            id=task_id,
            title=title,
            path=rel,
            task_type=defaults.task_type,
            executor=defaults.executor,
            depends_on=(),
            allowed_scope=tuple(allowed_scope),
            out_of_scope=tuple(defaults.out_of_scope),
            required_skills=tuple(defaults.required_skills),
            max_repair_attempts=defaults.max_repair_attempts,
            documentation_impact=tuple(defaults.documentation_impact),
            verification_commands=tuple(defaults.verification_commands),
            acceptance_criteria=criteria,
            metadata_source="defaults",
            defaults_applied=tuple(applied),
        )
    except SchemaError as exc:
        raise TaskFileError(f"{rel}: {exc}") from None


# --- narrow, idempotent '## Blockers' section edits (VR-03) -------------------------------


def render_blocker_entry(
    key: str, reason: str, *, fields: Mapping[str, object] | None = None
) -> list[str]:
    """One ``- [key] reason`` bullet plus an indented ``  - name: value`` line per field."""
    entry = [f"- [{key}] {reason}".rstrip()]
    for name, value in (fields or {}).items():
        entry.append(f"  - {name}: {value}")
    return entry


def _blocker_groups(section: Sequence[str]) -> list[list[str]]:
    """Split a ``## Blockers`` section body into one list of lines per ``- [..]`` entry."""
    groups: list[list[str]] = []
    current: list[str] | None = None
    for line in section:
        if _BLOCKER_KEY_RE.match(line):
            current = [line]
            groups.append(current)
        elif current is not None and (not line.strip() or line.startswith((" ", "\t"))):
            current.append(line)
        elif current is not None:
            current.append(line)
    for group in groups:
        while group and not group[-1].strip():
            group.pop()
    return groups


def upsert_blockers_section(
    path: str | Path,
    key: str,
    reason: str,
    *,
    fields: Mapping[str, object] | None = None,
) -> bool:
    """Add or replace exactly one ``[key]`` entry under the task file's ``## Blockers`` section.

    The section is created at end of file when absent. An entry whose key already matches is
    replaced in place; any other entry is preserved verbatim. Nothing outside the
    ``## Blockers`` section is touched — the ``## Status`` and ``## Acceptance Criteria``
    checkboxes are never rewritten. Returns ``True`` when the file content changed.
    """
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()
    entry = render_blocker_entry(key, reason, fields=fields)

    start = next(
        (i for i, line in enumerate(lines) if line.strip().lower() == BLOCKERS_HEADING.lower()),
        None,
    )
    if start is None:
        prefix = lines[:]
        if prefix and prefix[-1].strip():
            prefix.append("")
        rebuilt = prefix + [BLOCKERS_HEADING, "", *entry, ""]
    else:
        end = next(
            (j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")),
            len(lines),
        )
        groups = _blocker_groups(lines[start + 1:end])
        replaced = False
        for group in groups:
            match = _BLOCKER_KEY_RE.match(group[0])
            if match and match.group("key") == key:
                group[:] = entry
                replaced = True
        if not replaced:
            groups.append(entry)
        body: list[str] = [""]
        for group in groups:
            body.extend(group)
            body.append("")
        rebuilt = lines[:start + 1] + body + lines[end:]

    document = newline.join(line.rstrip() for line in rebuilt).rstrip(newline).rstrip("\n")
    document += newline
    if document == original:
        return False
    path.write_text(document, encoding="utf-8")
    return True
