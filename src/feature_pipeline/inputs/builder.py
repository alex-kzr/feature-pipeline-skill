"""The single builder every plan source goes through (IN-01).

``TaskDefinitionBuilder`` turns any supported source — a native Markdown task file, a JSON
plan entry, a historical block-less file, a parsed legacy mapping — into one immutable
:class:`feature_pipeline.domain.models.TaskDefinition`. It owns:

* the shared metadata vocabulary and command parser (:mod:`.metadata`, :mod:`.commands`);
* the declared/defaulted resolution rules (which fields a *declared* source must carry,
  which a block-less source may take from caller :class:`SourceDefaults`, and what is
  recorded in ``defaults_applied``);
* the one fail-closed validation pass, delegated to
  :class:`feature_pipeline.contracts.TaskSpec`, so no run artifact or executor process is
  created for an invalid task.

The shipped entry points (``pipeline_core.task_files.load_task_spec``,
``pipeline_core.legacy_adapter.adapt_task_spec`` / ``adapt_legacy_task``) are thin facades
over this class and return byte-identical results (AC-3).

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from feature_pipeline.contracts import (
    AcceptanceCriterionSpec,
    SchemaError,
    TaskSpec,
    validate_acceptance_criteria,
)
from feature_pipeline.domain.models import (
    JSON_PLAN_ENTRY,
    MARKDOWN_TASK_FILE,
    TaskDefinition,
)

from .commands import commands_from_objects
from .metadata import (
    LIST_FIELDS,
    REQUIRED_DECLARED_FIELDS,
    is_none,
    parse_markdown_metadata_block,
    split_list,
    strip_backticks,
)

__all__ = ["SourceDefaults", "TaskDefinitionBuilder"]

#: Fields the normalized contract still lets a *declared* source omit (everything else that
#: is not in :data:`REQUIRED_DECLARED_FIELDS` is historical-only).
_DECLARED_DEFAULTABLE = ("max_repair_attempts", "blocking_conditions")

_HEADING_RE = re.compile(r"^#\s+(?P<id>[A-Za-z]{2,6}-\d{1,4})\s*[-—–]\s*(?P<title>.+?)\s*$")
_FILENAME_ID_RE = re.compile(r"^(?P<id>[A-Za-z]{2,6}-\d{1,4})_.+\.md$")
_AC_RE = re.compile(r"^-\s+\[(?P<done>[ xX])\]\s+(?P<id>AC-\d+)\s*[-—–]\s*(?P<text>.+?)\s*$")
_CHECKBOX_RE = re.compile(r"^-\s+\[(?P<done>[ xX])\]\s+(?P<text>.+?)\s*$")
_BACKTICK_TOKEN_RE = re.compile(r"`([^`]+)`")


@dataclass(frozen=True)
class SourceDefaults:
    """Caller-supplied values for a source that declares no execution metadata.

    Unifies the historical ``pipeline_core.task_files.TaskDefaults`` and
    ``pipeline_core.legacy_adapter.LegacyDefaults`` — nothing here is inferred by the core.
    """

    task_type: str
    executor: str
    max_repair_attempts: int = 2
    out_of_scope: tuple[str, ...] = ()
    required_skills: tuple[str, ...] = ()
    documentation_impact: tuple[str, ...] = ()
    verification_commands: tuple[object, ...] = ()


@dataclass
class _Resolved:
    """Normalized field values, still source-neutral, before ``TaskSpec`` validation."""

    task_id: str
    title: str
    path: str
    task_type: str
    executor: str
    depends_on: tuple[object, ...]
    allowed_scope: tuple[object, ...]
    out_of_scope: tuple[object, ...]
    required_skills: tuple[object, ...]
    max_repair_attempts: int
    documentation_impact: tuple[object, ...]
    verification_commands: tuple[object, ...]
    verification_tier: str
    accepts_scoped: tuple[object, ...]
    deferred_verification_commands: tuple[object, ...]
    blocking_conditions: str | None
    acceptance_criteria: tuple[AcceptanceCriterionSpec, ...]
    metadata_source: str
    defaults_applied: tuple[str, ...]
    unknown_fields: tuple[str, ...] = ()


class TaskDefinitionBuilder:
    """Named constructors — one per source shape — that all land on one ``TaskDefinition``."""

    # --- native Markdown task file ------------------------------------------------------

    @classmethod
    def from_markdown_task_file(
        cls,
        path: str | Path,
        *,
        defaults: "SourceDefaults | None" = None,
        error: type[Exception] = SchemaError,
    ) -> TaskDefinition:
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        resolved = cls._resolve_markdown(
            text, rel=path.as_posix(), filename=path.name,
            defaults=defaults, error=error,
        )
        return cls._finish(resolved, MARKDOWN_TASK_FILE, error=error)

    # --- JSON plan entry / parsed legacy mapping --------------------------------------

    @classmethod
    def from_json_plan_entry(
        cls,
        entry: Mapping[str, object],
        *,
        defaults: "SourceDefaults | None" = None,
        error: type[Exception] = SchemaError,
    ) -> TaskDefinition:
        resolved = cls.resolve_mapping(entry, defaults=defaults, error=error)
        return cls._finish(resolved, JSON_PLAN_ENTRY, error=error)

    # --- shared resolution: parsed mapping ------------------------------------------------

    @classmethod
    def resolve_mapping(
        cls,
        raw: Mapping[str, object],
        *,
        defaults: "SourceDefaults | None",
        error: type[Exception] = SchemaError,
    ) -> _Resolved:
        """Resolve a parsed task mapping, applying ``defaults`` only for a block-less source.

        This is the canonical form of ``pipeline_core.legacy_adapter.adapt_legacy_task`` — a
        declared entry keeps every declared value and may only default the two
        contract-permitted absentees; a historical entry takes the full default set and
        reports each key it used in ``defaults_applied``. ``raw`` is read, never modified.
        """
        task_id = str(raw.get("id") or "").strip()
        has_metadata = bool(raw.get("has_metadata"))
        if not has_metadata and defaults is None:
            raise error(
                f"{task_id or '<task>'}: execution metadata is absent and no defaults were "
                f"supplied"
            )
        if not task_id:
            raise error("task mapping has no 'id'")

        applied: list[str] = []
        effective = defaults or SourceDefaults(task_type="", executor="")

        def resolve(key: str, declared: object, default: object) -> object:
            if declared is not None:
                return declared
            if has_metadata and key not in _DECLARED_DEFAULTABLE:
                raise error(
                    f"{task_id}: declared metadata is missing required field '{key}'"
                )
            applied.append(key)
            return default

        task_type = str(resolve("type", raw.get("type"), effective.task_type))
        executor = str(resolve("executor", raw.get("executor"), effective.executor))
        allowed_scope = _as_tuple(
            resolve("allowed_scope", raw.get("allowed_scope"), raw.get("affected_files")),
            error=error,
        )
        if not allowed_scope:
            raise error(f"{task_id}: no allowed scope could be resolved")
        out_of_scope = _as_tuple(
            resolve("out_of_scope", raw.get("out_of_scope"), effective.out_of_scope),
            error=error,
        )
        documentation_impact = _as_tuple(
            resolve("documentation_impact", raw.get("documentation_impact"),
                    effective.documentation_impact),
            error=error,
        )
        verification_commands = _as_tuple(
            resolve("verification_commands", raw.get("verification_commands"),
                    effective.verification_commands),
            error=error,
        )
        repair_bound = resolve(
            "max_repair_attempts", raw.get("max_repair_attempts"),
            effective.max_repair_attempts,
        )
        max_repair_attempts = int(repair_bound)  # type: ignore[call-overload]

        depends_on = _as_tuple(raw.get("depends_on"), error=error)
        blocking_conditions = raw.get("blocking_conditions")
        blocking = None if blocking_conditions is None else str(blocking_conditions)

        criteria_source = raw.get("acceptance_criteria")
        try:
            if criteria_source is not None:
                criteria = validate_acceptance_criteria(
                    criteria_source  # type: ignore[arg-type]
                )
            else:
                criteria = validate_acceptance_criteria(
                    _as_tuple(raw.get("definition_of_done"), error=error)
                )
                if not has_metadata:
                    applied.append("acceptance_criteria")
        except SchemaError as exc:
            if error is SchemaError or isinstance(exc, error):
                raise
            raise error(str(exc)) from None

        return _Resolved(
            task_id=task_id,
            title=str(raw.get("title") or "").strip(),
            path=str(raw.get("path") or ""),
            task_type=task_type,
            executor=executor,
            depends_on=depends_on,
            allowed_scope=allowed_scope,
            out_of_scope=out_of_scope,
            required_skills=_as_tuple(raw.get("required_skills"), error=error),
            max_repair_attempts=max_repair_attempts,
            documentation_impact=documentation_impact,
            verification_commands=verification_commands,
            verification_tier=str(raw.get("verification_tier") or "full"),
            accepts_scoped=_as_tuple(raw.get("accepts_scoped"), error=error),
            deferred_verification_commands=_as_tuple(
                raw.get("deferred_verification_commands"), error=error
            ),
            blocking_conditions=blocking,
            acceptance_criteria=criteria,
            metadata_source="declared" if has_metadata else "defaults",
            defaults_applied=tuple(applied),
        )

    # --- shared resolution: Markdown task file ------------------------------------------

    @classmethod
    def _resolve_markdown(
        cls,
        text: str,
        *,
        rel: str,
        filename: str,
        defaults: "SourceDefaults | None",
        error: type[Exception],
    ) -> _Resolved:
        lines = text.splitlines()
        task_id, title = cls._identity(lines, filename, error)
        raw = parse_markdown_metadata_block(text, error=error)
        criteria, synthesized = cls._acceptance_criteria(lines, error)

        if raw is None:
            if defaults is None:
                raise error(
                    f"{rel}: no '## Execution Metadata' block and no defaults supplied — a "
                    f"historical task needs explicit caller defaults to execute"
                )
            scope = cls._affected_paths(lines)
            if not scope:
                raise error(
                    f"{rel}: no '## Affected Files / Components' paths to use as the "
                    f"allowed scope"
                )
            applied = ["task_type", "executor", "out_of_scope", "required_skills",
                       "documentation_impact", "verification_commands",
                       "max_repair_attempts"]
            if synthesized:
                applied.append("acceptance_criteria")
            return _Resolved(
                task_id=task_id,
                title=title,
                path=rel,
                task_type=defaults.task_type,
                executor=defaults.executor,
                depends_on=(),
                allowed_scope=tuple(scope),
                out_of_scope=tuple(defaults.out_of_scope),
                required_skills=tuple(defaults.required_skills),
                max_repair_attempts=defaults.max_repair_attempts,
                documentation_impact=tuple(defaults.documentation_impact),
                verification_commands=tuple(defaults.verification_commands),
                verification_tier="full",
                accepts_scoped=(),
                deferred_verification_commands=(),
                blocking_conditions=None,
                acceptance_criteria=criteria,
                metadata_source="defaults",
                defaults_applied=tuple(applied),
            )

        fields = raw.fields
        for key in REQUIRED_DECLARED_FIELDS:
            if key not in fields:
                raise error(
                    f"{rel}: declared '## Execution Metadata' is missing required field "
                    f"'{key.replace('_', ' ')}'"
                )

        lists: dict[str, tuple[str, ...]] = {
            key: tuple(split_list(fields[key])) for key in LIST_FIELDS if key in fields
        }

        max_repair = 2
        if "max_repair_attempts" in fields:
            token = strip_backticks(fields["max_repair_attempts"])
            if not token.isdigit():
                raise error(
                    f"{rel}: 'Maximum repair attempts' must be a non-negative integer, got "
                    f"'{fields['max_repair_attempts']}'"
                )
            max_repair = int(token)

        tier = "full"
        if "verification_tier" in fields:
            tier = strip_backticks(fields["verification_tier"]).lower()

        blocking: str | None = None
        if "blocking_conditions" in fields:
            blocking = None if is_none(fields["blocking_conditions"]) else \
                fields["blocking_conditions"]

        return _Resolved(
            task_id=task_id,
            title=title,
            path=rel,
            task_type=fields["task_type"],
            executor=fields["executor"],
            depends_on=lists.get("depends_on", ()),
            allowed_scope=lists.get("allowed_scope", ()),
            out_of_scope=lists.get("out_of_scope", ()),
            required_skills=lists.get("required_skills", ()),
            max_repair_attempts=max_repair,
            documentation_impact=lists.get("documentation_impact", ()),
            verification_commands=tuple(
                {"cwd": cwd, "command": command} for cwd, command in raw.commands
            ),
            verification_tier=tier,
            accepts_scoped=lists.get("accepts_scoped", ()),
            deferred_verification_commands=tuple(
                {"cwd": cwd, "command": command} for cwd, command in raw.deferred
            ),
            blocking_conditions=blocking,
            acceptance_criteria=criteria,
            metadata_source="declared",
            defaults_applied=(),
        )

    # --- TaskSpec validation + wrap ---------------------------------------------------

    @classmethod
    def _finish(
        cls, resolved: _Resolved, source_format: str, *, error: type[Exception]
    ) -> TaskDefinition:
        try:
            spec = TaskSpec.build(
                id=resolved.task_id,
                title=resolved.title,
                path=resolved.path,
                task_type=resolved.task_type,
                executor=resolved.executor,
                depends_on=resolved.depends_on,
                allowed_scope=resolved.allowed_scope,
                out_of_scope=resolved.out_of_scope,
                required_skills=resolved.required_skills,
                max_repair_attempts=resolved.max_repair_attempts,
                documentation_impact=resolved.documentation_impact,
                verification_commands=commands_from_objects(
                    resolved.verification_commands
                ),
                verification_tier=resolved.verification_tier,
                accepts_scoped=resolved.accepts_scoped,
                deferred_verification_commands=commands_from_objects(
                    resolved.deferred_verification_commands,
                    field="deferred_verification_commands",
                ),
                blocking_conditions=resolved.blocking_conditions,
                acceptance_criteria=resolved.acceptance_criteria,
                metadata_source=resolved.metadata_source,
                defaults_applied=resolved.defaults_applied,
            )
        except SchemaError as exc:
            if error is SchemaError or isinstance(exc, error):
                raise
            raise error(f"{resolved.path or resolved.task_id}: {exc}") from None
        return TaskDefinition(
            spec=spec,
            source_format=source_format,
            unknown_fields=resolved.unknown_fields,
        )

    # --- Markdown helpers (ported verbatim from pipeline_core.task_files) ---------------

    @staticmethod
    def _identity(
        lines: Sequence[str], filename: str, error: type[Exception]
    ) -> tuple[str, str]:
        for line in lines:
            match = _HEADING_RE.match(line)
            if match:
                return match.group("id"), match.group("title").strip()
        filename_match = _FILENAME_ID_RE.match(filename)
        if filename_match:
            return filename_match.group("id"), ""
        raise error(
            f"{filename}: no '# <TASK-ID> - <title>' heading and the filename does not "
            f"carry an ID"
        )

    @staticmethod
    def _acceptance_criteria(
        lines: Sequence[str], error: type[Exception]
    ) -> tuple[tuple[AcceptanceCriterionSpec, ...], bool]:
        def has(heading: str) -> bool:
            return any(
                line.startswith("## ") and line.strip().lower() == heading
                for line in lines
            )

        def section(heading: str) -> list[str]:
            out: list[str] = []
            inside = False
            for line in lines:
                if line.startswith("## "):
                    inside = line.strip().lower() == heading
                    continue
                if inside:
                    out.append(line)
            return out

        if has("## acceptance criteria"):
            parsed: list[dict[str, object]] = []
            for line in section("## acceptance criteria"):
                match = _AC_RE.match(line.strip())
                if match:
                    parsed.append({
                        "id": match.group("id"),
                        "text": match.group("text").strip(),
                        "checked": match.group("done").lower() == "x",
                    })
            if not parsed:
                raise error("'## Acceptance Criteria' is present but lists no criteria")
            try:
                return validate_acceptance_criteria(parsed), False
            except SchemaError as exc:
                raise error(str(exc)) from None

        dod: list[str] = []
        for line in section("## definition of done"):
            match = _CHECKBOX_RE.match(line.strip())
            if match:
                dod.append(match.group("text").strip())
        if dod:
            try:
                return validate_acceptance_criteria(dod), True
            except SchemaError as exc:
                raise error(str(exc)) from None
        return (), False

    @staticmethod
    def _affected_paths(lines: Sequence[str]) -> list[str]:
        paths: list[str] = []
        inside = False
        for line in lines:
            if line.startswith("## "):
                inside = line.strip().lower() == "## affected files / components"
                continue
            if not inside:
                continue
            for token in _BACKTICK_TOKEN_RE.findall(line):
                candidate = token.strip().replace("\\", "/")
                if candidate and not candidate.startswith(("~", "/")):
                    paths.append(candidate)
        return paths


def _as_tuple(
    value: object, *, error: type[Exception] = SchemaError
) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        raise error(f"expected a list, got {type(value).__name__}")
    return tuple(value)  # type: ignore[arg-type]
