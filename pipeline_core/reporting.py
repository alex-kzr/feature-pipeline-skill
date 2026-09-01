"""Minimal Markdown report assembly shared by the execution-engine artifact writers.

Deliberately small: the full Reporter/logger split is deferred. This exists so a diagnostic
and any later report render their sections in one place, with a stable heading shape and no
trailing whitespace (``git diff --check`` stays clean).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Section:
    """One ``##`` section of a report."""

    title: str
    body: str


def fenced(body: str, language: str = "text") -> str:
    """Wrap text in a fenced code block, guaranteeing a non-empty body."""
    return f"```{language}\n{body or 'none'}\n```"


def render_report(title: str, sections: Sequence[Section]) -> str:
    """Render ``# title`` followed by each section, newline-terminated, no trailing spaces."""
    lines: list[str] = [f"# {title}", ""]
    for section in sections:
        lines += [f"## {section.title}", "", *section.body.split("\n"), ""]
    trimmed = "\n".join(line.rstrip() for line in lines).rstrip("\n")
    return trimmed + "\n"
