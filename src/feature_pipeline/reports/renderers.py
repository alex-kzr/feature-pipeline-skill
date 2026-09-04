"""Compatibility Markdown renderers for the report protocol (VP-02).

VP-02 makes the typed settlement path (``settlement`` / ``findings`` / ``dispositions``) the
source of truth, but every persisted artifact keeps its historical filename and byte layout so
the compatibility goldens stay green. These renderers are the one place that turns typed
values back into that Markdown.

:func:`render_report` / :func:`fenced` / :class:`Section` are lifted verbatim from
``pipeline_core.reporting`` — which now re-exports them from here — so a diagnostic and any
later report render with the same stable heading shape and no trailing whitespace
(``git diff --check`` stays clean).

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .findings import Finding


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


def render_findings_block(findings: Sequence[Finding], *, empty: str = "none") -> str:
    """A stable one-bullet-per-finding block for embedding in a compatibility report."""
    rows = [
        f"- [{finding.severity}] {finding.code}: {finding.message}"
        for finding in findings
    ]
    return "\n".join(rows) if rows else f"- {empty}"
