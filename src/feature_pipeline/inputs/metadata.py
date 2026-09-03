"""The one metadata field vocabulary and Markdown ``## Execution Metadata`` parser (IN-01).

Both the native Markdown task-file reader (``pipeline_core.task_files``) and — through the
builder — every other source share this module. It owns:

* :data:`FIELD_ALIASES` — the Markdown field label → normalized key map;
* :data:`REQUIRED_DECLARED_FIELDS` / :data:`LIST_FIELDS` / :data:`SUBLIST_FIELDS`;
* the value helpers :func:`is_none`, :func:`strip_backticks`, :func:`split_list`;
* :func:`parse_markdown_metadata_block` — the fail-closed block parser, previously
  ``pipeline_core.task_files.parse_execution_metadata`` verbatim, now parameterised only by
  the exception type a caller wants raised.

Behaviour is unchanged from the pre-IN-01 reader: an unknown field, a duplicate field, an
inline value on a sub-list field, an empty field, or an unparseable command reference all
fail closed.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Type

from feature_pipeline.contracts import SchemaError

__all__ = [
    "FIELD_ALIASES",
    "REQUIRED_DECLARED_FIELDS",
    "LIST_FIELDS",
    "SUBLIST_FIELDS",
    "RawTaskMetadata",
    "canonical_field",
    "is_none",
    "strip_backticks",
    "split_list",
    "parse_markdown_metadata_block",
]

#: Markdown field name (lower-cased, whitespace-collapsed) -> normalized key.
FIELD_ALIASES: dict[str, str] = {
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

#: Keys a *declared* metadata block must carry; every other key may be defaulted.
REQUIRED_DECLARED_FIELDS: tuple[str, ...] = (
    "task_type", "executor", "depends_on", "allowed_scope", "out_of_scope",
    "required_skills", "documentation_impact", "verification_commands",
)

LIST_FIELDS: tuple[str, ...] = (
    "depends_on", "allowed_scope", "out_of_scope", "required_skills",
    "documentation_impact", "accepts_scoped",
)

SUBLIST_FIELDS: tuple[str, ...] = (
    "verification_commands", "deferred_verification_commands",
)

_META_FIELD_RE = re.compile(r"^-\s+(?P<key>[A-Za-z][A-Za-z ]*?)\s*:\s*(?P<value>.*)$")
_COMMAND_RE = re.compile(
    r"^\s+-\s+`?(?P<cwd>[^`]+?)`?\s*(?:->|→)\s*`?(?P<command>.+?)`?\s*$"
)


def canonical_field(label: str) -> str | None:
    """The normalized key for a Markdown field label, or ``None`` if it is not recognised."""
    return FIELD_ALIASES.get(" ".join(label.strip().lower().split()))


def is_none(value: str) -> bool:
    return value.strip().lower() == "none"


def strip_backticks(value: str) -> str:
    token = value.strip()
    if len(token) > 1 and token.startswith("`") and token.endswith("`"):
        token = token[1:-1]
    return token.strip()


def split_list(value: str) -> list[str]:
    if is_none(value):
        return []
    return [strip_backticks(part) for part in value.split(",") if part.strip()]


def _has_section(lines: list[str], heading: str) -> bool:
    return any(line.startswith("## ") and line.strip().lower() == heading.strip().lower()
              for line in lines)


def _section(lines: list[str], heading: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    inside = False
    for index, line in enumerate(lines, start=1):
        if line.startswith("## "):
            inside = line.strip().lower() == heading.strip().lower()
            continue
        if inside:
            out.append((index, line))
    return out


@dataclass
class RawTaskMetadata:
    """The unparsed-but-recognised ``## Execution Metadata`` fields.

    ``fields`` holds every scalar/list field as written (a string); ``commands`` and
    ``deferred`` hold ``(cwd, command)`` pairs for the two sub-list fields. Semantic
    validation (path safety, shell-free argv, task-type membership, …) happens later, in the
    builder, through :class:`feature_pipeline.contracts.TaskSpec`.
    """

    fields: dict[str, str] = field(default_factory=dict)
    commands: list[tuple[str, str]] = field(default_factory=list)
    deferred: list[tuple[str, str]] = field(default_factory=list)

    def as_legacy_mapping(self) -> dict[str, object]:
        """The dict shape ``pipeline_core.task_files.parse_execution_metadata`` returned."""
        return {**self.fields, "_commands": self.commands, "_deferred": self.deferred}


def parse_markdown_metadata_block(
    text: str, *, error: Type[Exception] = SchemaError
) -> RawTaskMetadata | None:
    """Parse a task file's ``## Execution Metadata`` block, or ``None`` when it is absent.

    ``error`` is the exception type raised on any malformed field so a caller (e.g.
    ``pipeline_core.task_files``) keeps raising its own ``TaskFileError``.
    """
    lines = text.splitlines()
    if not _has_section(lines, "## Execution Metadata"):
        return None
    section = _section(lines, "## Execution Metadata")

    raw = RawTaskMetadata()
    seen: dict[str, int] = {}
    current: str | None = None

    for line_no, source_line in section:
        if not source_line.strip():
            continue
        if source_line.startswith((" ", "\t")) and current in SUBLIST_FIELDS:
            match = _COMMAND_RE.match(source_line)
            if not match:
                raise error(
                    f"line {line_no}: unparseable verification command "
                    f"'{source_line.strip()}' — expected '`<cwd>` -> `<command>`'"
                )
            target = raw.commands if current == "verification_commands" else raw.deferred
            target.append((match.group("cwd").strip(), match.group("command").strip()))
            continue

        field_match = _META_FIELD_RE.match(source_line)
        if not field_match:
            continue
        label = field_match.group("key").strip()
        key = canonical_field(label)
        if key is None:
            raise error(f"line {line_no}: unknown metadata field '{label}'")
        if key in seen:
            raise error(
                f"line {line_no}: duplicate field '{label}' (first at line {seen[key]})"
            )
        seen[key] = line_no
        current = key
        value = field_match.group("value").strip()
        if key in SUBLIST_FIELDS:
            if value and not is_none(value):
                raise error(
                    f"line {line_no}: '{label}' takes an indented sub-list, not an inline "
                    f"value"
                )
            raw.fields[key] = "none" if is_none(value) else ""
            continue
        if not value:
            raise error(
                f"line {line_no}: field '{label}' is empty — write 'none' to state that "
                f"explicitly"
            )
        raw.fields[key] = value

    return raw
