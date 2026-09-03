"""Read-only migration adapter for legacy task metadata.

Historical task files predate the ``## Execution Metadata`` block. The contract for them is
"extend, do not convert": they stay executable through documented defaults and are never
rewritten just to look uniform.

Since **IN-01** the resolution rules live in one place —
:meth:`feature_pipeline.inputs.TaskDefinitionBuilder.resolve_mapping` — and this module is a
delegating facade:

* :func:`adapt_legacy_task` projects the shared resolution onto the loose in-memory
  :class:`AdaptedTask` view (raw command objects preserved, ``LegacyAdapterError`` raised);
* :func:`adapt_task_spec` is the JSON counterpart of
  :func:`pipeline_core.task_files.load_task_spec` — a Markdown task file and a JSON plan
  entry describing the same task land on an equivalent, fully validated ``TaskSpec``.

Neither performs file I/O and neither mutates its input: ``raw`` is read, never modified.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from feature_pipeline.contracts import SchemaError, TaskSpec
from feature_pipeline.inputs import SourceDefaults, TaskDefinitionBuilder


class LegacyAdapterError(ValueError):
    """A legacy task mapping that cannot be adapted without guessing."""


#: Fields the contract still lets a *declared* task omit; every other default is historical-only.
DECLARED_DEFAULTABLE = ("max_repair_attempts", "blocking_conditions")


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    text: str
    checked: bool = False


@dataclass(frozen=True)
class LegacyDefaults:
    """Project-supplied defaults for a task file that carries no metadata block."""

    task_type: str
    executor: str
    max_repair_attempts: int = 2
    out_of_scope: tuple[str, ...] = ()
    documentation_impact: tuple[str, ...] = ()
    verification_commands: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class AdaptedTask:
    """One legacy task normalized in memory. Nothing here is written back to disk."""

    task_id: str
    title: str
    task_type: str
    executor: str
    depends_on: tuple[str, ...]
    allowed_scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    max_repair_attempts: int
    documentation_impact: tuple[str, ...]
    verification_commands: tuple[Mapping[str, object], ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    blocking_conditions: str | None
    metadata_source: str
    defaults_applied: tuple[str, ...] = field(default_factory=tuple)


def synthesize_acceptance_criteria(items: Sequence[object]) -> tuple[AcceptanceCriterion, ...]:
    """Assign synthetic ``AC-1 … AC-n`` IDs to ordered ``Definition of Done`` entries.

    Each entry is either a string (unchecked) or a ``{"text": ..., "checked": ...}`` mapping.
    The numbering is in-memory only; the source file is not touched.
    """
    criteria: list[AcceptanceCriterion] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, Mapping):
            text = str(item.get("text", "")).strip()
            checked = bool(item.get("checked", False))
        else:
            text, checked = str(item).strip(), False
        if not text:
            raise LegacyAdapterError(f"definition-of-done entry {index} has no text")
        criteria.append(AcceptanceCriterion(f"AC-{index}", text, checked))
    return tuple(criteria)


def _source_defaults(defaults: "LegacyDefaults | SourceDefaults | None") -> SourceDefaults | None:
    if defaults is None or isinstance(defaults, SourceDefaults):
        return defaults
    return SourceDefaults(
        task_type=defaults.task_type,
        executor=defaults.executor,
        max_repair_attempts=defaults.max_repair_attempts,
        out_of_scope=tuple(defaults.out_of_scope),
        documentation_impact=tuple(defaults.documentation_impact),
        verification_commands=tuple(defaults.verification_commands),
    )


def adapt_legacy_task(raw: Mapping[str, object], defaults: LegacyDefaults) -> AdaptedTask:
    """Normalize one parsed task mapping, applying ``defaults`` only for a block-less file.

    Delegates the declared/defaulted resolution to the shared builder and projects the
    result onto :class:`AdaptedTask`. A file that declared an ``## Execution Metadata`` block
    (``raw["has_metadata"]`` truthy) keeps every declared value and may only default
    ``max_repair_attempts`` / ``blocking_conditions``; a historical file takes the full
    default set and reports each key it used in ``defaults_applied``.
    """
    resolved = TaskDefinitionBuilder.resolve_mapping(
        raw, defaults=_source_defaults(defaults), error=LegacyAdapterError
    )
    return AdaptedTask(
        task_id=resolved.task_id,
        title=resolved.title,
        task_type=resolved.task_type,
        executor=resolved.executor,
        depends_on=tuple(resolved.depends_on),
        allowed_scope=tuple(resolved.allowed_scope),
        out_of_scope=tuple(resolved.out_of_scope),
        max_repair_attempts=resolved.max_repair_attempts,
        documentation_impact=tuple(resolved.documentation_impact),
        verification_commands=tuple(resolved.verification_commands),
        acceptance_criteria=tuple(
            AcceptanceCriterion(item.id, item.text, item.checked)
            for item in resolved.acceptance_criteria
        ),
        blocking_conditions=resolved.blocking_conditions,
        metadata_source=resolved.metadata_source,
        defaults_applied=tuple(resolved.defaults_applied),
    )


def adapt_task_spec(
    raw: Mapping[str, object], defaults: "LegacyDefaults | None" = None
) -> TaskSpec:
    """Normalize a parsed JSON plan entry (or any parsed task mapping) into a ``TaskSpec``.

    The JSON counterpart of :func:`pipeline_core.task_files.load_task_spec`, sharing the one
    metadata/command parser and the one validation pass. An entry with ``has_metadata`` false
    and no ``defaults`` fails closed. ``raw`` is read, never modified.
    """
    return TaskDefinitionBuilder.from_json_plan_entry(
        raw, defaults=_source_defaults(defaults), error=SchemaError
    ).to_task_spec()
