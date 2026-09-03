"""The one verification-command parser (IN-01).

Every source normalizes a command the same way: through
:class:`feature_pipeline.contracts.CommandSpec`, which folds a ``{"cwd", "argv"}`` object, a
``{"cwd", "command"}`` string, or a bare :class:`CommandSpec` into a working directory plus a
validated shell-free argv. This module adds only the Markdown surface syntax
(``` `<cwd>` -> `<command>` ```) on top of that single validator.

Standard library only.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from feature_pipeline.contracts import CommandSpec

__all__ = [
    "MARKDOWN_COMMAND_RE",
    "parse_markdown_command_line",
    "command_from_reference",
    "command_from_object",
    "commands_from_pairs",
    "commands_from_objects",
]

#: A Markdown verification-command bullet: ``  - `<cwd>` -> `<command>` `` (``→`` accepted).
MARKDOWN_COMMAND_RE = re.compile(
    r"^\s+-\s+`?(?P<cwd>[^`]+?)`?\s*(?:->|→)\s*`?(?P<command>.+?)`?\s*$"
)


def parse_markdown_command_line(line: str) -> tuple[str, str] | None:
    """``(cwd, command)`` for a command bullet, or ``None`` when the line is not one."""
    match = MARKDOWN_COMMAND_RE.match(line)
    if not match:
        return None
    return match.group("cwd").strip(), match.group("command").strip()


def command_from_reference(
    cwd: str, command: str, *, field: str = "verification_commands"
) -> CommandSpec:
    """A :class:`CommandSpec` from a Markdown ``(cwd, command)`` pair."""
    return CommandSpec.from_data({"cwd": cwd, "command": command}, field=field)


def command_from_object(
    data: object, *, field: str = "verification_commands"
) -> CommandSpec:
    """A :class:`CommandSpec` from a JSON/legacy object (or a passthrough ``CommandSpec``)."""
    return CommandSpec.from_data(data, field=field)


def commands_from_pairs(
    pairs: Iterable[tuple[str, str]], *, field: str = "verification_commands"
) -> tuple[CommandSpec, ...]:
    return tuple(command_from_reference(cwd, command, field=field) for cwd, command in pairs)


def commands_from_objects(
    items: Iterable[object] | None, *, field: str = "verification_commands"
) -> tuple[CommandSpec, ...]:
    if not items:
        return ()
    if isinstance(items, Mapping):  # a lone object, not a list of them
        return (command_from_object(items, field=field),)
    return tuple(command_from_object(item, field=field) for item in items)
