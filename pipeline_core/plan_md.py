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

Convention 2 also reads an optional ``## Supersession`` section from each task file (a bare
``Supersedes`` column is honoured in a convention-1 table): the task IDs a task declares it
supersedes. A verified replacement can then satisfy a dependency on the blocked predecessor
without that predecessor being rewritten. See :mod:`pipeline_core.supersession`.

Fails closed with :class:`MarkdownPlanError` on: neither convention yielding a task, a task
missing an ID or a Type, a missing or unreadable task file (convention 2), a duplicate task
ID, a dependency naming an ID absent from the plan, a task that depends on itself, or an
invalid, cyclic, or ambiguous supersession declaration.

Standard library only.
"""

from __future__ import annotations

import re
from pathlib import Path

from feature_pipeline.contracts import SchemaError, TaskSpec

from .supersession import Supersession, SupersessionError, SupersessionGraph
from .task_files import TaskDefaults, load_task_spec
from .preconditions import parse_preconditions

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
_SUPERSEDE_HEADERS = {"supersedes", "supersede", "supersession", "supersedes_ids"}

_SUPERSEDES_FIELD_RE = re.compile(r"^-\s*supersedes\s*:\s*(?P<value>.*)$", re.IGNORECASE)
_SUPERSEDES_BULLET_RE = re.compile(r"^-\s*(?P<id>[A-Za-z]{2,6}-\d{1,4})\b")


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
            elif name in _SUPERSEDE_HEADERS:
                found.setdefault("supersedes", position)
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
                      "depends_on": _split_deps(value("depends_on")),
                      "supersedes": _split_deps(value("supersedes"))})
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


def _supersession_ids(task_file: Path) -> list[str]:
    """Task IDs from a task file's dedicated ``## Supersession`` section.

    This section is owned by the portable core alone (like ``## Blockers``); it is *not* an
    ``## Execution Metadata`` field, so a task that supersedes a blocked predecessor never
    rewrites that predecessor and never widens the closed metadata vocabulary. Accepts either
    a ``- Supersedes: <ids>`` line or a bullet list of bare IDs.
    """
    out: list[str] = []
    in_block = False
    for line in task_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_block = stripped.lower() == "## supersession"
            continue
        if not in_block or not stripped:
            continue
        field = _SUPERSEDES_FIELD_RE.match(stripped)
        if field:
            for token in _split_deps(field.group("value")):
                if token not in out:
                    out.append(token)
            continue
        bullet = _SUPERSEDES_BULLET_RE.match(stripped)
        if bullet and bullet.group("id") not in out:
            out.append(bullet.group("id"))
    return out


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
                      "depends_on": _split_deps(fields.get("depends on", "")),
                      "supersedes": _supersession_ids(matches[0])})
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
        matches = sorted((path.parent / "tasks").glob(f"{task['id']}_*.md"))
        if matches:
            try:
                predicates = parse_preconditions(matches[0].read_text(encoding="utf-8"))
            except SchemaError as exc:
                raise MarkdownPlanError(f"{task['id']}: {exc}") from None
            if predicates:
                task["preconditions"] = predicates
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

    for task in tasks:
        task.setdefault("supersedes", [])
    try:
        SupersessionGraph.from_tasks(tasks)
    except SupersessionError as exc:
        raise MarkdownPlanError(str(exc)) from None

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
    supersedes_by_id: dict[str, list[str]] = {}
    for task_id in ids:
        matches = sorted(tasks_dir.glob(f"{task_id}_*.md"))
        if not matches:
            raise MarkdownPlanError(
                f"{task_id}: no task file '{task_id}_*.md' under {tasks_dir.name}/ to load "
                f"execution metadata from"
            )
        try:
            parse_preconditions(matches[0].read_text(encoding="utf-8"))
        except SchemaError as exc:
            raise MarkdownPlanError(f"{task_id}: {exc}") from None
        specs.append(load_task_spec(matches[0], defaults=defaults))
        supersedes_by_id[task_id] = _supersession_ids(matches[0])

    known = {spec.id for spec in specs}
    for spec in specs:
        if spec.id in spec.depends_on:
            raise MarkdownPlanError(f"{spec.id} depends on itself")
        for dependency in spec.depends_on:
            if dependency not in known:
                raise MarkdownPlanError(
                    f"{spec.id} depends on '{dependency}', which is not a task in the plan"
                )

    try:
        SupersessionGraph(
            [
                Supersession(replacement=spec.id, superseded=superseded)
                for spec in specs
                for superseded in supersedes_by_id.get(spec.id, ())
            ],
            known_ids=[spec.id for spec in specs],
            dependencies={spec.id: list(spec.depends_on) for spec in specs},
        )
    except SupersessionError as exc:
        raise MarkdownPlanError(str(exc)) from None

    return feature, tuple(specs)
