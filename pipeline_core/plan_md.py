"""Minimal Markdown-plan reader for the portable core CLI.

A JSON plan (``--plan foo.json``) is the core's native format. This reader lets
``--plan foo.md`` drive the same run construction from a Markdown plan - a common
task-board plan format - so the core can be pointed at a real project's plan without a
separate JSON conversion step.

Two Markdown conventions are accepted, tried in order:

1. **Task table.** A pipe table whose header row names, case-insensitively, an ``ID``
   column, a ``Type`` column, and a ``Depends on`` column (``depends_on`` /
   ``dependencies`` / ``dependency`` / ``depends`` also accepted). Column order is free;
   any other column is ignored.

2. **Phase headings + task files.** Failing a table, every ``### <ID>`` heading under a
   ``## Phase`` section is a task, and its ``Type`` / ``Depends on`` come from the
   ``## Execution Metadata`` block of ``<plan-dir>/tasks/<ID>_*.md``.

In both conventions: the ID and Type are read verbatim after stripping surrounding
backticks and any ``[text](link)`` wrapper; a ``Depends on`` value of ``(none)`` / ``-`` /
an em/en dash / empty means no dependency, otherwise it is split on commas and whitespace
into dependency IDs. The feature name is the plan filename stem with a leading
``YYYY-MM-DD-`` stripped, else a slug of the first ``# Feature: <name>`` heading;
``--feature`` still overrides it.

Fails closed with :class:`MarkdownPlanError` on: neither convention yielding a task, a task
missing an ID or a Type, a missing or unreadable task file (convention 2), a duplicate task
ID, a dependency naming an ID absent from the plan, or a task that depends on itself.

Standard library only.
"""

from __future__ import annotations

import re
from pathlib import Path

from schemas.contracts import TaskSpec

from .task_files import TaskDefaults, load_task_spec

__all__ = ["MarkdownPlanError", "load_markdown_plan", "load_markdown_plan_specs"]


class MarkdownPlanError(Exception):
    """The Markdown plan does not meet the reader's contract."""


_LINK_RE = re.compile(r"^\[(?P<text>[^\]]+)\]\([^)]*\)$")
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
_SEPARATOR_CELL_RE = re.compile(r":?-{3,}:?")
_DEP_SPLIT_RE = re.compile(r"[,\s]+")
_FEATURE_HEADING_RE = re.compile(r"^#\s+Feature:\s*(.+?)\s*$", re.MULTILINE)
_TASK_HEADING_RE = re.compile(r"^###\s+(?P<id>[A-Za-z]{2,6}-\d{1,4})\b")
_META_FIELD_RE = re.compile(r"^-\s*(?P<key>[A-Za-z ]+?)\s*:\s*(?P<value>.*)$")

_NONE_TOKENS = {"", "-", "—", "–", "(none)", "none", "n/a"}
_ID_HEADERS = {"id", "task", "task id"}
_TYPE_HEADERS = {"type", "task type"}
_DEP_HEADERS = {"depends on", "depends_on", "dependencies", "dependency", "depends"}


def _cells(line: str) -> list[str]:
    """Split one Markdown table line into trimmed cell strings, or ``[]`` if it is not a row."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _clean(cell: str) -> str:
    value = cell.strip().strip("`").strip()
    link = _LINK_RE.match(value)
    if link:
        value = link.group("text").strip().strip("`").strip()
    return value


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(_SEPARATOR_CELL_RE.fullmatch(cell or "") for cell in cells)


def _split_deps(raw: str) -> list[str]:
    if raw.strip().lower() in _NONE_TOKENS:
        return []
    out: list[str] = []
    for token in _DEP_SPLIT_RE.split(raw):
        cleaned = _clean(token)
        if cleaned and cleaned.lower() not in _NONE_TOKENS:
            out.append(cleaned)
    return out


def _feature_name(path: Path, text: str) -> str:
    stem = _DATE_PREFIX_RE.sub("", path.stem).strip()
    if stem:
        return stem
    heading = _FEATURE_HEADING_RE.search(text)
    if heading:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", heading.group(1).strip()).strip("-").lower()
        if slug:
            return slug
    raise MarkdownPlanError(
        "cannot derive a feature name from the plan filename or a '# Feature:' heading"
    )


# --- convention 1: task table -------------------------------------------------------------


def _table_tasks(lines: list[str]) -> list[dict] | None:
    header_index = columns = None
    for index, line in enumerate(lines):
        header = [cell.lower() for cell in _cells(line)]
        if len(header) < 2 or index + 1 >= len(lines):
            continue
        if not _is_separator(_cells(lines[index + 1])):
            continue
        found: dict[str, int] = {}
        for position, name in enumerate(header):
            if name in _ID_HEADERS:
                found.setdefault("id", position)
            elif name in _TYPE_HEADERS:
                found.setdefault("type", position)
            elif name in _DEP_HEADERS:
                found.setdefault("depends_on", position)
        if "id" in found and "type" in found:
            header_index, columns = index, found
            break
    if header_index is None:
        return None

    tasks: list[dict] = []
    for line in lines[header_index + 2:]:
        cells = _cells(line)
        if not cells:
            break
        if _is_separator(cells):
            continue

        def value(key: str) -> str:
            position = columns.get(key)
            if position is None or position >= len(cells):
                return ""
            return _clean(cells[position])

        task_id, task_type = value("id"), value("type")
        if not task_id or not task_type:
            raise MarkdownPlanError(
                f"a task row is missing an ID or a Type: {line.strip()!r}"
            )
        tasks.append({"id": task_id, "type": task_type,
                      "depends_on": _split_deps(value("depends_on"))})
    return tasks


# --- convention 2: phase headings + task files ------------------------------------------


def _meta_block(task_file: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    in_block = False
    for line in task_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_block:
                break
            in_block = stripped.lower() == "## execution metadata"
            continue
        if not in_block:
            continue
        match = _META_FIELD_RE.match(stripped)
        if match:
            fields[match.group("key").strip().lower()] = match.group("value").strip()
    return fields


def _heading_tasks(lines: list[str], plan_path: Path) -> list[dict] | None:
    ids: list[str] = []
    for line in lines:
        heading = _TASK_HEADING_RE.match(line.strip())
        if heading and heading.group("id") not in ids:
            ids.append(heading.group("id"))
    if not ids:
        return None

    tasks_dir = plan_path.parent / "tasks"
    tasks: list[dict] = []
    for task_id in ids:
        matches = sorted(tasks_dir.glob(f"{task_id}_*.md"))
        if not matches:
            raise MarkdownPlanError(
                f"{task_id}: no task file '{task_id}_*.md' under {tasks_dir.name}/"
            )
        fields = _meta_block(matches[0])
        task_type = _clean(fields.get("type", ""))
        if not task_type:
            raise MarkdownPlanError(
                f"{task_id}: task file has no 'Type' in its '## Execution Metadata' block"
            )
        tasks.append({"id": task_id, "type": task_type,
                      "depends_on": _split_deps(fields.get("depends on", ""))})
    return tasks


# --- entry point -----------------------------------------------------------------------


def load_markdown_plan(path: Path) -> tuple[str, list[dict]]:
    """Return ``(feature, tasks)`` from a Markdown plan; ``tasks`` matches the JSON plan shape."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MarkdownPlanError("plan file not found") from None
    except OSError:
        raise MarkdownPlanError("plan file is not readable") from None

    lines = text.splitlines()
    tasks = _table_tasks(lines)
    if tasks is None:
        tasks = _heading_tasks(lines, path)
    if not tasks:
        raise MarkdownPlanError(
            "no tasks found: need a '| ID | Type | Depends on |' table or '### <ID>' "
            "headings with matching tasks/<ID>_*.md files"
        )

    seen: set[str] = set()
    for task in tasks:
        if task["id"] in seen:
            raise MarkdownPlanError(f"duplicate task id in plan: {task['id']}")
        seen.add(task["id"])

    ids = set(seen)
    for task in tasks:
        if task["id"] in task["depends_on"]:
            raise MarkdownPlanError(f"{task['id']} depends on itself")
        for dependency in task["depends_on"]:
            if dependency not in ids:
                raise MarkdownPlanError(
                    f"{task['id']} depends on '{dependency}', which is not a task in the plan"
                )

    return _feature_name(path, text), tasks


def load_markdown_plan_specs(
    path: Path, *, defaults: TaskDefaults | None = None
) -> tuple[str, tuple[TaskSpec, ...]]:
    """Return ``(feature, task_specs)`` — every task normalized to a validated :class:`TaskSpec`.

    This is the execution-grade counterpart of :func:`load_markdown_plan`: the same feature name
    and task order, but each task is resolved to its ``tasks/<ID>_*.md`` file and normalized
    through :func:`pipeline_core.task_files.load_task_spec`. A plan that carries no per-task
    files (a bare ``| ID | Type | Depends on |`` table) has no execution metadata and fails
    closed with :class:`MarkdownPlanError`; a block-less task file fails closed too unless
    ``defaults`` is supplied for it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MarkdownPlanError("plan file not found") from None
    except OSError:
        raise MarkdownPlanError("plan file is not readable") from None

    ids: list[str] = []
    for line in text.splitlines():
        heading = _TASK_HEADING_RE.match(line.strip())
        if heading and heading.group("id") not in ids:
            ids.append(heading.group("id"))
    if not ids:
        raise MarkdownPlanError(
            "no '### <ID>' task headings: a bare task table carries no execution metadata, so "
            "it cannot drive a real run — use a plan with tasks/<ID>_*.md task files"
        )

    feature = _feature_name(path, text)
    tasks_dir = path.parent / "tasks"

    specs: list[TaskSpec] = []
    for task_id in ids:
        matches = sorted(tasks_dir.glob(f"{task_id}_*.md"))
        if not matches:
            raise MarkdownPlanError(
                f"{task_id}: no task file '{task_id}_*.md' under {tasks_dir.name}/ to load "
                f"execution metadata from"
            )
        specs.append(load_task_spec(matches[0], defaults=defaults))

    known = {spec.id for spec in specs}
    for spec in specs:
        if spec.id in spec.depends_on:
            raise MarkdownPlanError(f"{spec.id} depends on itself")
        for dependency in spec.depends_on:
            if dependency not in known:
                raise MarkdownPlanError(
                    f"{spec.id} depends on '{dependency}', which is not a task in the plan"
                )

    return feature, tuple(specs)
