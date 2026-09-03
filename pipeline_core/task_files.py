"""Native Markdown task-file reader for the portable core.

Since **IN-01** this module is a thin facade over
:mod:`feature_pipeline.inputs`: a task file's ``## Execution Metadata`` and
``## Acceptance Criteria`` blocks are parsed by the one shared metadata/command parser and
normalized by the one :class:`~feature_pipeline.inputs.TaskDefinitionBuilder` into a
validated :class:`~feature_pipeline.contracts.TaskSpec` — the same model a JSON plan entry
produces through :func:`pipeline_core.legacy_adapter.adapt_task_spec`. Historical task files
that predate the metadata block stay executable through caller-supplied :class:`TaskDefaults`;
the source file is only ever read, never rewritten.

Validation is fail-closed and happens before any run artifact or executor process exists: an
unknown field, an absent required field, an unsafe path, a shell operator in a command, or
non-sequential acceptance-criteria IDs all raise :class:`TaskFileError`.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from feature_pipeline.contracts import (
    AcceptanceCriterionSpec,
    CommandSpec,
    SchemaError,
    TaskSpec,
    validate_acceptance_criteria,
)
from feature_pipeline.inputs import (
    SourceDefaults,
    TaskDefinitionBuilder,
    parse_markdown_metadata_block,
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
    what a historical file's type, executor, and checks are. Passed straight through to
    :class:`feature_pipeline.inputs.SourceDefaults`.
    """

    task_type: str
    executor: str
    max_repair_attempts: int = 2
    out_of_scope: tuple[str, ...] = ()
    required_skills: tuple[str, ...] = ()
    documentation_impact: tuple[str, ...] = ()
    verification_commands: tuple[CommandSpec, ...] = ()


def parse_execution_metadata(text: str) -> dict[str, object] | None:
    """The raw ``## Execution Metadata`` fields, or ``None`` when the block is absent.

    Delegates to the shared parser (:func:`feature_pipeline.inputs.parse_markdown_metadata_block`)
    and returns its historical dict shape — scalar/list fields as written, plus
    ``_commands`` / ``_deferred`` lists of ``(cwd, command)`` pairs. Semantic validation
    happens in :func:`load_task_spec`.
    """
    raw = parse_markdown_metadata_block(text, error=TaskFileError)
    return None if raw is None else raw.as_legacy_mapping()


def synthesize_acceptance_criteria(
    items: Sequence[object],
) -> tuple[AcceptanceCriterionSpec, ...]:
    """Assign synthetic ``AC-1 … AC-n`` IDs to ordered ``Definition of Done`` entries."""
    try:
        return validate_acceptance_criteria(items)
    except SchemaError as exc:
        raise TaskFileError(str(exc)) from None


def load_task_spec(path: Path, *, defaults: TaskDefaults | None = None) -> TaskSpec:
    """Normalize one Markdown task file into a validated :class:`TaskSpec`.

    A file with an ``## Execution Metadata`` block is normalized from that block alone;
    ``defaults`` is only consulted for a historical, block-less file. When such a file is
    given and ``defaults`` is ``None`` the call fails closed — real execution never runs a
    task whose metadata is neither declared nor supplied.
    """
    source_defaults: SourceDefaults | None = (
        None
        if defaults is None
        else SourceDefaults(
            task_type=defaults.task_type,
            executor=defaults.executor,
            max_repair_attempts=defaults.max_repair_attempts,
            out_of_scope=tuple(defaults.out_of_scope),
            required_skills=tuple(defaults.required_skills),
            documentation_impact=tuple(defaults.documentation_impact),
            verification_commands=tuple(defaults.verification_commands),
        )
    )
    return TaskDefinitionBuilder.from_markdown_task_file(
        Path(path), defaults=source_defaults, error=TaskFileError
    ).to_task_spec()


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
